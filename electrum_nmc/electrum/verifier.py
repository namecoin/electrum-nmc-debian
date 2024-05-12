# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2012 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
from typing import Sequence, Optional, TYPE_CHECKING
from math import ceil, log

import aiorpcx

from .util import bh2u, TxMinedInfo, NetworkJobOnDefaultServer
from .crypto import sha256d
from .bitcoin import hash_decode, hash_encode
from .transaction import Transaction
from .blockchain import hash_header
from .interface import GracefulDisconnect
from .network import UntrustedServerReturnedError
from .merkle import hash_merkle_root, MerkleVerificationFailure, MerkleRootMismatch
from . import constants

if TYPE_CHECKING:
    from .network import Network
    from .address_synchronizer import AddressSynchronizer


class MissingBlockHeader(MerkleVerificationFailure): pass


class SPV(NetworkJobOnDefaultServer):
    """ Simple Payment Verification """

    def __init__(self, network: 'Network', wallet: Optional['AddressSynchronizer']):
        self.wallet = wallet
        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self.merkle_roots = {}  # txid -> merkle root (once it has been verified)
        self.requested_merkle = set()  # txid set of pending requests

    async def _start_tasks(self):
        async with self.taskgroup as group:
            await group.spawn(self.main)

    def diagnostic_name(self):
        if self.wallet is not None:
            return self.wallet.diagnostic_name()
        return "SPV"

    async def main(self):
        self.blockchain = self.network.blockchain()
        while True:
            if self.wallet is not None:
                await self._maybe_undo_verifications()
                await self._request_proofs()
            await asyncio.sleep(0.1)

    async def _request_proofs(self):
        local_height = self.blockchain.height()
        unverified = self.wallet.get_unverified_txs()

        for tx_hash, tx_height in unverified.items():
            # do not request merkle branch if we already requested it
            if tx_hash in self.requested_merkle or tx_hash in self.merkle_roots:
                continue
            # or before headers are available
            if tx_height <= 0 or tx_height > local_height:
                continue
            # if it's in the checkpoint region, we still might not have the header
            use_individual_header_proof = False
            header = self.blockchain.read_header(tx_height)
            if header is None:
                if tx_height < constants.net.max_checkpoint():
                    # Calculate whether a chunk download or individual header
                    # downloads will be more bandwidth-efficient...
                    headers_in_chunk_period = len(set([height for (_, height) in unverified.items() if height // 2016 == tx_height // 2016]))
                    if is_chunk_cheaper(headers_in_chunk_period):
                        self.logger.info(f'downloading full chunk for tx {tx_hash} at height {tx_height} because individual header is less efficient')
                        await self.taskgroup.spawn(self.network.request_chunk(tx_height, None, can_return_early=True))
                    else:
                        self.logger.info(f'skipping chunk for tx {tx_hash} at height {tx_height} because individual header is more efficient')
                        use_individual_header_proof = True
                if not use_individual_header_proof:
                    continue
            # request now
            self.logger.info(f'requested merkle {tx_hash}')
            self.requested_merkle.add(tx_hash)
            await self.taskgroup.spawn(self._request_and_verify_single_proof, tx_hash, tx_height, use_individual_header_proof)

    async def _request_and_verify_single_proof(self, tx_hash, tx_height, use_individual_header_proof=False, stream_id=None):
        try:
            merkle_getter = self.network.get_merkle_for_transaction(tx_hash, tx_height, stream_id=stream_id)
            if use_individual_header_proof:
                interface = self.network.get_interface_for_stream_id(stream_id)
                if interface is None:
                    raise Exception("No clean interface is ready")
                header_getter = interface.get_block_header(tx_height, 'SPV verifier', must_provide_proof=True)
                merkle, (header, proof_was_provided) = await asyncio.gather(merkle_getter, header_getter)
            else:
                merkle = await merkle_getter
        except UntrustedServerReturnedError as e:
            if not isinstance(e.original_exception, aiorpcx.jsonrpc.RPCError) or self.wallet is None:
                raise
            self.logger.info(f'tx {tx_hash} not at height {tx_height}')
            self.wallet.remove_unverified_tx(tx_hash, tx_height)
            self.requested_merkle.discard(tx_hash)
            return
        # Verify the hash of the server-provided merkle branch to a
        # transaction matches the merkle root of its block
        if tx_height != merkle.get('block_height'):
            self.logger.info('requested tx_height {} differs from received tx_height {} for txid {}'
                             .format(tx_height, merkle.get('block_height'), tx_hash))
        tx_height = merkle.get('block_height')
        pos = merkle.get('pos')
        merkle_branch = merkle.get('merkle')
        if not use_individual_header_proof:
            # TODO: This logic will work instantly if the header is available
            # at the start, but will wait for a full syncup otherwise, even if
            # the header becomes available almost immediately.  Can we improve
            # on this?
            header = self.network.blockchain().read_header(tx_height)
            if header is None:
                # we need to wait if header sync/reorg is still ongoing, hence lock:
                async with self.network.bhi_lock:
                    header = self.network.blockchain().read_header(tx_height)
        try:
            verify_tx_is_in_block(tx_hash, merkle_branch, pos, header, tx_height)
        except MerkleVerificationFailure as e:
            if self.network.config.get("skipmerklecheck"):
                self.logger.info(f"skipping merkle proof check {tx_hash}")
            elif self.wallet is None:
                raise
            else:
                self.logger.info(repr(e))
                raise GracefulDisconnect(e) from e
        # we passed all the tests
        self.merkle_roots[tx_hash] = header.get('merkle_root')
        self.requested_merkle.discard(tx_hash)
        self.logger.info(f"verified {tx_hash}")
        if self.wallet is None:
            return
        header_hash = hash_header(header)
        tx_info = TxMinedInfo(height=tx_height,
                              timestamp=header.get('timestamp'),
                              txpos=pos,
                              header_hash=header_hash)
        self.wallet.add_verified_tx(tx_hash, tx_info)
        #if self.is_up_to_date() and self.wallet.is_up_to_date():
        #    self.wallet.save_verified_tx(write=True)

    async def _maybe_undo_verifications(self):
        old_chain = self.blockchain
        cur_chain = self.network.blockchain()
        if cur_chain != old_chain:
            self.blockchain = cur_chain
            above_height = cur_chain.get_height_of_last_common_block_with_chain(old_chain)
            self.logger.info(f"undoing verifications above height {above_height}")
            tx_hashes = self.wallet.undo_verifications(self.blockchain, above_height)
            for tx_hash in tx_hashes:
                self.logger.info(f"redoing {tx_hash}")
                self.remove_spv_proof_for_tx(tx_hash)

    def remove_spv_proof_for_tx(self, tx_hash):
        self.merkle_roots.pop(tx_hash, None)
        self.requested_merkle.discard(tx_hash)

    def is_up_to_date(self):
        return not self.requested_merkle


def is_chunk_cheaper(headers_in_chunk_period: int) -> bool:
    # 32 bytes per hash
    branch_len = 32 * ceil(log(constants.net.max_checkpoint() + 1, 2))
    root_len = 32
    bare_header_len = 80

    chunk_len = 2016 * bare_header_len + branch_len + root_len
    individual_headers_len = headers_in_chunk_period * (branch_len + root_len + bare_header_len)

    return chunk_len < individual_headers_len

def verify_tx_is_in_block(tx_hash: str, merkle_branch: Sequence[str],
                          leaf_pos_in_tree: int, block_header: Optional[dict],
                          block_height: int) -> None:
    """Raise MerkleVerificationFailure if verification fails."""
    if not block_header:
        raise MissingBlockHeader("merkle verification failed for {} (missing header {})"
                                 .format(tx_hash, block_height))
    if len(merkle_branch) > 30:
        raise MerkleVerificationFailure(f"merkle branch too long: {len(merkle_branch)}")
    calc_merkle_root = hash_merkle_root(merkle_branch, tx_hash, leaf_pos_in_tree)
    if block_header.get('merkle_root') != calc_merkle_root:
        raise MerkleRootMismatch("merkle verification failed for {} ({} != {})".format(
            tx_hash, block_header.get('merkle_root'), calc_merkle_root))
