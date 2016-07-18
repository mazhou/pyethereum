import time
from ethereum import utils
from ethereum.utils import parse_as_bin, big_endian_to_int
from ethereum import parse_genesis_declaration
from ethereum.state_transition import apply_block, initialize, \
    finalize, apply_transaction, mk_receipt_sha, mk_transaction_sha, \
    calc_difficulty, calc_gaslimit, Receipt, mk_receipt
import rlp
from rlp.utils import encode_hex
from ethereum.exceptions import InvalidNonce, InsufficientStartGas, UnsignedTransactionError, \
    BlockGasLimitReached, InsufficientBalance
from ethereum.slogging import get_logger
from ethereum.config import Env
from ethereum.state import State, dict_to_prev_header
from ethereum.block import Block, BlockHeader, FakeHeader, BLANK_UNCLES_HASH
import random
import json
log = get_logger('eth.chain')


class Chain(object):

    def __init__(self, genesis=None, env=None, coinbase=b'\x00' * 20, **kwargs):
        self.env = env or Env()
        # Initialize the state
        if 'head_hash' in self.db:
            self.state = self.mk_poststate_of_blockhash(self.db.get('head_hash'))
            print 'Initializing chain from saved head, #%d (%s)' % \
                (self.state.prev_headers[0].number, encode_hex(self.state.prev_headers[0].hash))
        elif genesis is None:
            raise Exception("Need genesis decl!")
        elif isinstance(genesis, State):
            self.state = genesis
            print 'Initializing chain from provided state'
        elif "extraData" in genesis:
            self.state = parse_genesis_declaration.state_from_genesis_declaration(
                genesis, self.env)
            print 'Initializing chain from provided genesis declaration'
        elif "prev_headers" in genesis:
            self.state = State.from_snapshot(genesis, self.env)
            print 'Initializing chain from provided state snapshot, %d (%s)' % \
                (self.state.block_number, encode_hex(self.state.prev_headers[0].hash[:8]))
        else:
            print 'Initializing chain from new state based on alloc'
            self.state = parse_genesis_declaration.mk_basic_state(genesis, {
                "number": kwargs.get('number', 0),
                "gas_limit": kwargs.get('gas_limit', 4712388),
                "gas_used": kwargs.get('gas_used', 0),
                "timestamp": kwargs.get('timestamp', 1467446877),
                "difficulty": kwargs.get('difficulty', 2**25),
                "hash": kwargs.get('prevhash', '00' * 32),
                "uncles_hash": kwargs.get('uncles_hash', '0x' + encode_hex(BLANK_UNCLES_HASH))
            }, self.env)
        self.head_hash = self.state.prev_headers[0].hash
        self.db.put('GENESIS_NUMBER', str(self.state.block_number))
        self.db.put('score:' + self.state.prev_headers[0].hash, "0")
        self.db.put('GENESIS_STATE', json.dumps(self.state.to_snapshot()))
        self.db.put(self.head_hash, 'GENESIS')
        self.transaction_queue = []
        self.min_gasprice = kwargs.get('min_gasprice', 5 * 10**9)
        self.coinbase = coinbase
        self.extra_data = 'moo ha ha says the laughing cow.'
        self.time_queue = []
        self.parent_queue = {}

    @property
    def head(self):
        try:
            return rlp.decode(self.db.get(self.head_hash), Block)
        except:
            return None

    def mk_poststate_of_blockhash(self, blockhash):
        if blockhash not in self.db:
            raise Exception("Block hash %s not found" % encode_hex(blockhash))
        if self.db.get(blockhash) == 'GENESIS':
            return State.from_snapshot(json.loads(self.db.get('GENESIS_STATE')), self.env)
        state = State(env=self.env)
        block = rlp.decode(self.db.get(blockhash), Block)
        state.trie.root_hash = block.header.state_root
        initialize(state, block)
        state.gas_used = block.header.gas_used
        state.txindex = len(block.transactions)
        state.recent_uncles = {}
        state.prev_headers = []
        b = block
        for i in range(257):
            state.prev_headers.append(b.header)
            if i < 6:
                state.recent_uncles[state.block_number - i] = []
                for u in b.uncles:
                    state.recent_uncles[state.block_number - i].append(u.hash)
            try:
                b = rlp.decode(state.db.get(b.header.prevhash), Block)
            except:
                break
        if i < 256:
            if state.db.get(b.header.prevhash) == 'GENESIS':
                jsondata = json.loads(state.db.get('GENESIS_STATE'))
                for h in jsondata["prev_headers"][:257 - i - 1]:
                    state.prev_headers.append(dict_to_prev_header(h))
                for blknum, uncles in jsondata["recent_uncles"].items():
                    if blknum >= state.block_number - 6:
                        state.recent_uncles[blknum] = [parse_as_bin(u) for u in uncles]
            else:
                raise Exception("Dangling prevhash")
        return state

    def get_parent(self, block):
        if block.header.number == int(self.db.get('GENESIS_NUMBER')):
            return None
        return rlp.decode(self.db.get(block.header.prevhash), Block)

    def get_block(self, blockhash):
        try:
            return rlp.decode(self.db.get(blockhash), Block)
        except:
            return None

    # Add a record allowing you to later look up the provided block's
    # parent hash and see that it is one of its children
    def add_child(self, child):
        try:
            existing = self.db.get('child:' + child.header.prevhash)
        except:
            existing = ''
        self.db.put('child:' + child.header.prevhash, existing + child.header.hash)

    def get_blockhash_by_number(self, number):
        try:
            return self.db.get('block:' + str(number))
        except:
            return None

    def get_block_by_number(self, number):
        return self.get_block(self.get_blockhash_by_number(number))

    # Get the hashes of all known children of a given block
    def get_child_hashes(self, blockhash):
        o = []
        try:
            data = self.db.get('child:' + blockhash)
            for i in range(0, len(data), 32):
                o.append(data[i:i + 32])
            return o
        except:
            return []

    def get_children(self, block):
        if isinstance(block, Block):
            block = block.header.hash
        if isinstance(block, (BlockHeader, FakeHeader)):
            block = block.hash
        return [self.get_block(h) for h in self.get_child_hashes(block)]

    # Get the score (AKA total difficulty in PoW) of a given block
    def get_score(self, block):
        if not block:
            return 0
        key = 'score:' + block.header.hash
        if key not in self.db:
            try:
                parent_score = self.get_score(self.get_parent(block))
                self.db.put(key, str(parent_score + block.difficulty +
                                     random.randrange(block.difficulty // 10**6 + 1)))
            except:
                return int(self.db.get('score:' + block.prevhash))
        return int(self.db.get(key))

    # These two functions should be called periodically so as to
    # process blocks that were received but laid aside because
    # either the parent was missing or they were received
    # too early
    def process_time_queue(self):
        now = self.time()
        while len(self.time_queue) and self.time_queue[0].timestamp <= now:
            self.add_block(self.time_queue.pop())

    def process_parent_queue(self):
        for parent_hash, blocks in self.parent_queue:
            if parent_hash in self.db:
                for block in blocks:
                    self.add_block(block)
                del self.parent_queue[parent_hash]

    def time(self):
        return int(time.time())

    # Call upon receiving a block
    def add_block(self, block):
        now = self.time()
        if block.header.timestamp > now:
            i = 0
            while i < len(self.time_queue) and block.timestamp > self.time_queue[i].timestamp:
                i += 1
            self.time_queue.insert(i, block)
            print 'Block received too early. Delaying for now'
            return False
        if block.header.prevhash == self.head_hash:
            # print 'Adding to head', repr(block.header.prevhash), repr(self.head_hash)
            try:
                apply_block(self.state, block)
            # except Exception, e:  # FIXME catchall exception makes it unable to debug,
            except KeyError, e:  # FIXME add relevant exceptions here
                print 'Block %s with parent %s invalid' % (encode_hex(block.header.hash), encode_hex(block.header.prevhash))
                return False
            self.db.put('block:' + str(block.header.number), block.header.hash)
            self.head_hash = block.header.hash
            for i, tx in enumerate(block.transactions):
                self.db.put('txindex:' + tx.hash, rlp.encode([block.number, i]))
        elif block.header.prevhash in self.env.db:
            # print 'Receiving block not on head, adding to secondary post state'
            pre_state = self.mk_poststate_of_blockhash(block.header.prevhash)
            try:
                apply_block(pre_state, block)
            # except Exception, e:  # FIXME catchall exception makes it unable to debug,
            except KeyError, e:  # FIXME add relevant exceptions here
                print 'Block %s with parent %s invalid' % (encode_hex(block.hash), encode_hex(block.prevhash))
                return False
            block_score = self.get_score(block)
            # Replace the head
            if block_score > self.get_score(self.head):
                b = block
                new_chain = {}
                while b.header.number >= int(self.db.get('GENESIS_NUMBER')):
                    new_chain[b.header.number] = b
                    key = 'block:' + str(b.header.number)
                    orig_at_height = self.db.get(key) if key in self.db else None
                    if orig_at_height == b.header.hash:
                        break
                    if b.prevhash not in self.db or self.db.get(b.prevhash) == 'GENESIS':
                        break
                    b = self.get_parent(b)
                replace_from = b.header.number
                for i in xrange(replace_from, 2**63 - 1):
                    print 'Rewriting height %d' % i
                    key = 'block:' + str(i)
                    orig_at_height = self.db.get(key) if key in self.db else None
                    if orig_at_height:
                        self.db.delete(key)
                        orig_block_at_height = self.get_block(orig_at_height)
                        for tx in orig_block_at_height.transactions:
                            if 'txindex:' + tx.hash in self.db:
                                self.db.delete('txindex:' + tx.hash)
                    if i in new_chain:
                        new_block_at_height = new_chain[i]
                        self.db.put(key, new_block_at_height.header.hash)
                        for i, tx in enumerate(new_block_at_height.transactions):
                            self.db.put('txindex:' + tx.hash,
                                        rlp.encode([new_block_at_height.number, i]))
                    if i not in new_chain and not orig_at_height:
                        break
                self.head_hash = block.header.hash
                self.state = pre_state
        else:
            if block.header.prevhash not in self.parent_queue:
                self.parent_queue[block.header.prevhash] = []
            self.parent_queue[block.header.prevhash].append(block)
            print 'No parent found. Delaying for now'
            return False
        blk_txhashes = {tx.hash: True for tx in block.transactions}
        self.transaction_queue = [x for x in self.transaction_queue if x.hash in blk_txhashes]
        self.add_child(block)
        self.db.put('head_hash', self.head_hash)
        self.db.put(block.header.hash, rlp.encode(block))
        self.db.commit()
        print 'Added block %d (%s) with %d txs and %d gas' % \
            (block.header.number, encode_hex(block.header.hash)[:8],
             len(block.transactions), block.header.gas_used)
        return True

    def __contains__(self, blk):
        if isinstance(blk, (str, bytes)):
            try:
                blk = rlp.decode(self.db.get(blk), Block)
            except:
                return False
        try:
            o = self.get_block(self.get_blockhash_by_number(blk.number)).hash
            assert o == blk.hash
            return True
        except:
            return False

    def has_block(self, block):
        return block in self

    def get_chain(self, frm=None, to=2**63 - 1):
        if frm is None:
            frm = int(self.db.get('GENESIS_NUMBER')) + 1
        chain = []
        for i in xrange(frm, to):
            h = self.get_blockhash_by_number(i)
            if not h:
                return chain
            chain.append(self.get_block(h))

    # Recover transaction and the block that contains it
    def get_transaction(self, tx):
        if not isinstance(tx, (str, bytes)):
            tx = tx.hash
        if 'txindex:' + tx in self.db:
            data = rlp.decode(self.db.get('txindex:' + tx))
            blk, index = self.get_block_by_number(
                big_endian_to_int(data[0])), big_endian_to_int(data[1])
            tx = blk.transactions[index]
            return tx, blk, index
        else:
            return None

    # This should be called when a miner sees a transaction, and may
    # potentially be interested in including it in a block
    def add_transaction(self, tx):
        if tx.gasprice >= self.min_gasprice:
            i = 0
            while i < len(self.transaction_queue) and tx.gasprice < self.transaction_queue[i]:
                i += 1
            self.transaction_queue.insert(i, tx)
            print 'Added transaction to queue'
        else:
            print 'Gasprice too low!'

    # Get a transaction to include into a candidate block
    def get_candidate_transaction(self, gaslimit, excluded={}):
        i = 0
        while i < len(self.transaction_queue) and (self.transaction_queue[i].hash in excluded or
                                                   self.transaction_queue[i].startgas > gaslimit):
            i += 1
        return self.transaction_queue[i] if i < len(self.transaction_queue) else None

    # Make a candidate block for mining
    def make_head_candidate(self, parent=None, coinbase=None, timestamp=None):
        # clone the state so we can play with it without affecting the original
        if parent is None:
            temp_state = State.from_snapshot(self.state.to_snapshot(root_only=True), self.env)
        else:
            temp_state = self.mk_poststate_of_blockhash(parent.hash)
        blk = Block(BlockHeader())
        now = timestamp or self.time()
        blk.header.number = temp_state.block_number + 1
        blk.header.difficulty = calc_difficulty(temp_state.prev_headers[0], now, self.env.config)
        blk.header.gas_limit = calc_gaslimit(temp_state.prev_headers[0], self.env.config)
        blk.header.coinbase = coinbase or self.coinbase
        blk.header.timestamp = now
        blk.header.extra_data = self.extra_data
        blk.header.prevhash = temp_state.prev_headers[0].hash
        blk.header.bloom = 0
        blk.transactions = []
        blk.uncles = []
        receipts = []
        initialize(temp_state, blk)
        # Add transactions (highest fee first formula)
        excluded = {}
        while 1:
            tx = self.get_candidate_transaction(
                temp_state.gas_limit - temp_state.gas_used, excluded)
            if tx is None:
                break
            try:
                success, gas, logs = apply_transaction(temp_state, tx)
                r = mk_receipt(temp_state, logs)
                blk.transactions.append(tx)
                receipts.append(r)
                temp_state.bloom |= r.bloom  # int
            except (InsufficientBalance, BlockGasLimitReached, InsufficientStartGas,
                    InvalidNonce, UnsignedTransactionError):
                pass
            excluded[tx.hash] = True
        # Add uncles
        uncles = []
        ineligible = {}
        for h, uncles in temp_state.recent_uncles.items():
            for u in uncles:
                ineligible[u.hash] = True
        for i in range(0, min(6, len((temp_state.prev_headers)))):
            ineligible[temp_state.prev_headers[i].hash] = True
        for i in range(1, min(6, len(temp_state.prev_headers))):
            child_hashes = self.get_child_hashes(temp_state.prev_headers[i].hash)
            for c in child_hashes:
                if c not in ineligible and len(uncles) < 2:
                    uncles.append(self.get_block(c).header)
            if len(uncles) == 2:
                break
        blk.uncles = uncles
        finalize(temp_state, blk)
        blk.header.receipts_root = mk_receipt_sha(receipts)
        blk.header.tx_list_root = mk_transaction_sha(blk.transactions)
        blk.header.uncles_hash = utils.sha3(rlp.encode(blk.uncles))
        temp_state.commit()
        blk.header.state_root = temp_state.trie.root_hash
        blk.header.gas_used = temp_state.gas_used
        blk.header.bloom = temp_state.bloom
        return blk

    @property
    def db(self):
        return self.env.db

    @property
    def config(self):
        return self.env.config
