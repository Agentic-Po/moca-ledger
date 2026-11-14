"""Lossless, bounded state transport; runtime state remains ordinary JSON.

Readers accept legacy JSON and the versioned gzip envelope. Invalid compressed
state fails closed: never interpret an unreadable packet as an empty case list.
"""
import base64
import gzip
import hashlib
import json
import zlib

ENCODING = 'moca-state-gzip-v1'
MAX_DECODED_BYTES = 16 * 1024 * 1024


def _state(value):
    if not isinstance(value, dict) or ('open' in value and not isinstance(value['open'], dict)):
        raise ValueError('invalid state shape')
    return value


def encode_state(state):
    _state(state)
    if 'state_encoding' in state:
        raise ValueError('state already encoded')
    raw = json.dumps(state, separators=(',', ':'), ensure_ascii=False).encode()
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError('decoded state exceeds safety limit')
    envelope = {'state_encoding': ENCODING, 'decoded_bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(),
                'data': base64.b64encode(gzip.compress(raw, mtime=0)).decode()}
    return json.dumps(envelope, separators=(',', ':')).encode()


def decode_bytes(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_DECODED_BYTES:
        raise ValueError('state packet exceeds safety limit')
    envelope = _state(json.loads(raw))
    if 'state_encoding' not in envelope:
        return raw  # preserve legacy byte representation for local readers
    if envelope.get('state_encoding') != ENCODING or set(envelope) != {'state_encoding', 'decoded_bytes', 'sha256', 'data'}:
        raise ValueError('unsupported state envelope')
    size = envelope['decoded_bytes']
    if type(size) is not int or not 0 <= size <= MAX_DECODED_BYTES:
        raise ValueError('invalid decoded state size')
    packed = base64.b64decode(envelope['data'], validate=True)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = decoder.decompress(packed, size + 1)
    if len(result) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('invalid compressed state')
    if hashlib.sha256(result).hexdigest() != envelope['sha256']:
        raise ValueError('state checksum mismatch')
    state = _state(json.loads(result))
    if 'state_encoding' in state:
        raise ValueError('nested state envelope')
    return result


def decode_state(raw):
    return json.loads(decode_bytes(raw))
