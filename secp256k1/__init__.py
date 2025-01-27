import os
import hashlib
import binascii

from ._libsecp256k1 import ffi, lib


EC_COMPRESSED = lib.SECP256K1_EC_COMPRESSED
EC_UNCOMPRESSED = lib.SECP256K1_EC_UNCOMPRESSED

FLAG_SIGN = lib.SECP256K1_CONTEXT_SIGN
FLAG_VERIFY = lib.SECP256K1_CONTEXT_VERIFY
ALL_FLAGS = FLAG_SIGN | FLAG_VERIFY
NO_FLAGS = lib.SECP256K1_CONTEXT_NONE

HAS_RECOVERABLE = hasattr(lib, 'secp256k1_ecdsa_sign_recoverable')
HAS_SCHNORR = hasattr(lib, 'secp256k1_schnorrsig_sign')
HAS_ECDH = hasattr(lib, 'secp256k1_ecdh')
HAS_EXTRAKEYS = hasattr(lib, 'secp256k1_keypair_create')


class Base(object):

    def __init__(self, ctx, flags):
        self._destroy = None
        if ctx is None:
            assert flags in (NO_FLAGS, FLAG_SIGN, FLAG_VERIFY, ALL_FLAGS)
            ctx = lib.secp256k1_context_create(flags)
            self._destroy = lib.secp256k1_context_destroy

        self.flags = flags
        self.ctx = ctx

    def __del__(self):
        if not hasattr(self, '_destroy'):
            return

        if self._destroy and self.ctx:
            self._destroy(self.ctx)
            self.ctx = None

    def bip340_tag(self, msg, raw, tag):
        if raw:
            return msg

        if isinstance(tag, bytes):
            bytestag = tag
        else:
            bytestag = tag.encode()

        hash32 = ffi.new('char [32]')
        lib.secp256k1_tagged_sha256(self.ctx, hash32, bytestag, len(bytestag),
                                    msg, len(msg))
        return bytes(ffi.buffer(hash32, 32))


class ECDSA:  # Use as a mixin; instance.ctx is assumed to exist.

    def ecdsa_serialize(self, raw_sig):
        len_sig = 74
        output = ffi.new('unsigned char[%d]' % len_sig)
        outputlen = ffi.new('size_t *', len_sig)

        res = lib.secp256k1_ecdsa_signature_serialize_der(
            self.ctx, output, outputlen, raw_sig)
        assert res == 1

        return bytes(ffi.buffer(output, outputlen[0]))

    def ecdsa_deserialize(self, ser_sig):
        raw_sig = ffi.new('secp256k1_ecdsa_signature *')
        res = lib.secp256k1_ecdsa_signature_parse_der(
            self.ctx, raw_sig, ser_sig, len(ser_sig))
        assert res == 1

        return raw_sig

    def ecdsa_serialize_compact(self, raw_sig):
        len_sig = 64
        output = ffi.new('unsigned char[%d]' % len_sig)

        res = lib.secp256k1_ecdsa_signature_serialize_compact(
            self.ctx, output, raw_sig)
        assert res == 1

        return bytes(ffi.buffer(output, len_sig))

    def ecdsa_deserialize_compact(self, ser_sig):
        if len(ser_sig) != 64:
            raise Exception("invalid signature length")

        raw_sig = ffi.new('secp256k1_ecdsa_signature *')
        res = lib.secp256k1_ecdsa_signature_parse_compact(
            self.ctx, raw_sig, ser_sig)
        assert res == 1

        return raw_sig

    def ecdsa_signature_normalize(self, raw_sig, check_only=False):
        """
        Check and optionally convert a signature to a normalized lower-S form.
        If check_only is True then the normalized signature is not returned.

        This function always return a tuple containing a boolean (True if
        not previously normalized or False if signature was already
        normalized), and the normalized signature. When check_only is True,
        the normalized signature returned is always None.
        """
        if check_only:
            sigout = ffi.NULL
        else:
            sigout = ffi.new('secp256k1_ecdsa_signature *')

        result = lib.secp256k1_ecdsa_signature_normalize(
            self.ctx, sigout, raw_sig)

        return (bool(result), sigout if sigout != ffi.NULL else None)

    def ecdsa_recover(self, msg, recover_sig, raw=False,
                      digest=hashlib.sha256):
        if not HAS_RECOVERABLE:
            raise Exception("secp256k1_recovery not enabled")
        if self.flags & ALL_FLAGS != ALL_FLAGS:
            raise Exception("instance not configured for ecdsa recover")

        msg32 = _hash32(msg, raw, digest)
        pubkey = ffi.new('secp256k1_pubkey *')

        recovered = lib.secp256k1_ecdsa_recover(
            self.ctx, pubkey, recover_sig, msg32)
        if recovered:
            return pubkey
        raise Exception('failed to recover ECDSA public key')

    def ecdsa_recoverable_serialize(self, recover_sig):
        if not HAS_RECOVERABLE:
            raise Exception("secp256k1_recovery not enabled")

        outputlen = 64
        output = ffi.new('unsigned char[%d]' % outputlen)
        recid = ffi.new('int *')

        lib.secp256k1_ecdsa_recoverable_signature_serialize_compact(
            self.ctx, output, recid, recover_sig)

        return bytes(ffi.buffer(output, outputlen)), recid[0]

    def ecdsa_recoverable_deserialize(self, ser_sig, rec_id):
        if not HAS_RECOVERABLE:
            raise Exception("secp256k1_recovery not enabled")
        if rec_id < 0 or rec_id > 3:
            raise Exception("invalid rec_id")
        if len(ser_sig) != 64:
            raise Exception("invalid signature length")

        recover_sig = ffi.new('secp256k1_ecdsa_recoverable_signature *')

        parsed = lib.secp256k1_ecdsa_recoverable_signature_parse_compact(
            self.ctx, recover_sig, ser_sig, rec_id)
        if parsed:
            return recover_sig
        else:
            raise Exception('failed to parse ECDSA compact sig')

    def ecdsa_recoverable_convert(self, recover_sig):
        if not HAS_RECOVERABLE:
            raise Exception("secp256k1_recovery not enabled")

        normal_sig = ffi.new('secp256k1_ecdsa_signature *')

        lib.secp256k1_ecdsa_recoverable_signature_convert(
            self.ctx, normal_sig, recover_sig)

        return normal_sig


class PublicKey(Base, ECDSA):

    def __init__(self, pubkey=None, raw=False, flags=FLAG_VERIFY, ctx=None):
        Base.__init__(self, ctx, flags)
        if pubkey is not None:
            if raw:
                if not isinstance(pubkey, bytes):
                    raise TypeError('raw pubkey must be bytes')
                self.public_key = self.deserialize(pubkey)
            else:
                if not isinstance(pubkey, ffi.CData):
                    raise TypeError('pubkey must be an internal object')
                assert ffi.typeof(pubkey) is ffi.typeof('secp256k1_pubkey *')
                self.public_key = pubkey
            self._pubkey_changed()
        else:
            self.public_key = None

    def _pubkey_changed(self):
        if HAS_EXTRAKEYS:
            self.xonly_pubkey = ffi.new('secp256k1_xonly_pubkey *')
            assert lib.secp256k1_xonly_pubkey_from_pubkey(self.ctx,
                                                          self.xonly_pubkey,
                                                          ffi.NULL,
                                                          self.public_key) == 1

    def serialize(self, compressed=True):
        assert self.public_key, "No public key defined"

        len_compressed = 33 if compressed else 65
        res_compressed = ffi.new('char [%d]' % len_compressed)
        outlen = ffi.new('size_t *', len_compressed)
        compflag = EC_COMPRESSED if compressed else EC_UNCOMPRESSED

        serialized = lib.secp256k1_ec_pubkey_serialize(
            self.ctx, res_compressed, outlen, self.public_key, compflag)
        assert serialized == 1

        return bytes(ffi.buffer(res_compressed, len_compressed))

    def deserialize(self, pubkey_ser):
        if len(pubkey_ser) not in (33, 65):
            raise Exception("unknown public key size (expected 33 or 65)")

        pubkey = ffi.new('secp256k1_pubkey *')

        res = lib.secp256k1_ec_pubkey_parse(
            self.ctx, pubkey, pubkey_ser, len(pubkey_ser))
        if not res:
            raise Exception("invalid public key")

        self.public_key = pubkey
        self._pubkey_changed()
        return pubkey

    def combine(self, pubkeys):
        """Add a number of public keys together."""
        assert len(pubkeys) > 0

        outpub = ffi.new('secp256k1_pubkey *')
        for item in pubkeys:
            assert ffi.typeof(item) is ffi.typeof('secp256k1_pubkey *')

        res = lib.secp256k1_ec_pubkey_combine(
            self.ctx, outpub, pubkeys, len(pubkeys))
        if not res:
            raise Exception('failed to combine public keys')

        self.public_key = outpub
        self._pubkey_changed()
        return outpub

    def tweak_add(self, scalar):
        """
        Tweak the current public key by adding a 32 byte scalar times
        the generator to it and return a new PublicKey instance.
        """
        return _tweak_public(self, lib.secp256k1_ec_pubkey_tweak_add, scalar)

    def tweak_mul(self, scalar):
        """
        Tweak the current public key by multiplying it by a 32 byte scalar
        and return a new PublicKey instance.
        """
        return _tweak_public(self, lib.secp256k1_ec_pubkey_tweak_mul, scalar)

    def ecdsa_verify(self, msg, raw_sig, raw=False, digest=hashlib.sha256):
        assert self.public_key, "No public key defined"
        if self.flags & FLAG_VERIFY != FLAG_VERIFY:
            raise Exception("instance not configured for sig verification")

        msg32 = _hash32(msg, raw, digest)

        verified = lib.secp256k1_ecdsa_verify(
            self.ctx, raw_sig, msg32, self.public_key)

        return bool(verified)

    def schnorr_verify(self, msg, schnorr_sig, bip340tag, raw=False):
        assert self.public_key, "No public key defined"
        if not HAS_SCHNORR:
            raise Exception("secp256k1_schnorr not enabled")
        if self.flags & FLAG_VERIFY != FLAG_VERIFY:
            raise Exception("instance not configured for sig verification")

        msg_to_sign = self.bip340_tag(msg, raw, bip340tag)

        verified = lib.secp256k1_schnorrsig_verify(
            self.ctx, schnorr_sig, msg_to_sign, len(msg_to_sign),
            self.xonly_pubkey)

        return bool(verified)

    def ecdh(self, scalar, hashfn=ffi.NULL, hasharg=ffi.NULL):
        assert self.public_key, "No public key defined"
        if not HAS_ECDH:
            raise Exception("secp256k1_ecdh not enabled")
        # Technically, it need only match the hashfn, but this is standard.
        if not isinstance(scalar, bytes) or len(scalar) != 32:
            raise TypeError('scalar must be composed of 32 bytes')

        result = ffi.new('char [32]')

        res = lib.secp256k1_ecdh(self.ctx, result, self.public_key, scalar,
                                 hashfn, hasharg)
        if not res:
            raise Exception('invalid scalar ({})'.format(res))

        return bytes(ffi.buffer(result, 32))


class PrivateKey(Base, ECDSA):

    def __init__(self, privkey=None, raw=True, flags=ALL_FLAGS, ctx=None):
        assert flags in (ALL_FLAGS, FLAG_SIGN)

        Base.__init__(self, ctx, flags)
        self.pubkey = None
        self.private_key = None
        if privkey is None:
            self.set_raw_privkey(_gen_private_key())
        else:
            if raw:
                if not isinstance(privkey, bytes) or len(privkey) != 32:
                    raise TypeError('privkey must be composed of 32 bytes')
                self.set_raw_privkey(privkey)
            else:
                self.deserialize(privkey)

    def _update_public_key(self):
        public_key = self._gen_public_key(self.private_key)
        self.pubkey = PublicKey(
            public_key, raw=False, ctx=self.ctx, flags=self.flags)
        if HAS_EXTRAKEYS:
            self.keypair = ffi.new('secp256k1_keypair *')
            if lib.secp256k1_keypair_create(self.ctx,
                                            self.keypair,
                                            self.private_key) != 1:
                raise Exception("invalid private key (can't make keypair?)")

    def set_raw_privkey(self, privkey):
        if not lib.secp256k1_ec_seckey_verify(self.ctx, privkey):
            raise Exception("invalid private key")
        self.private_key = privkey
        self._update_public_key()

    def serialize(self):
        hexkey = binascii.hexlify(self.private_key)
        return hexkey.decode('utf8')

    def deserialize(self, privkey_ser):
        if len(privkey_ser) != 64:
            raise Exception("invalid private key")
        rawkey = binascii.unhexlify(privkey_ser)

        self.set_raw_privkey(rawkey)
        return self.private_key

    def _gen_public_key(self, privkey):
        pubkey_ptr = ffi.new('secp256k1_pubkey *')

        created = lib.secp256k1_ec_pubkey_create(self.ctx, pubkey_ptr, privkey)
        assert created == 1

        return pubkey_ptr

    def tweak_add(self, scalar):
        """
        Tweak the current private key by adding a 32 byte scalar
        to it and return a new raw private key composed of 32 bytes.
        """
        return _tweak_private(self, lib.secp256k1_ec_privkey_tweak_add, scalar)

    def tweak_mul(self, scalar):
        """
        Tweak the current private key by multiplying it by a 32 byte scalar
        and return a new raw private key composed of 32 bytes.
        """
        return _tweak_private(self, lib.secp256k1_ec_privkey_tweak_mul, scalar)

    def ecdsa_sign(self, msg, raw=False, digest=hashlib.sha256,
                   custom_nonce=None):
        msg32 = _hash32(msg, raw, digest)
        raw_sig = ffi.new('secp256k1_ecdsa_signature *')
        nonce_fn = ffi.NULL
        nonce_data = ffi.NULL
        if custom_nonce:
            nonce_fn, nonce_data = custom_nonce
        signed = lib.secp256k1_ecdsa_sign(
            self.ctx, raw_sig, msg32, self.private_key, nonce_fn, nonce_data)
        assert signed == 1

        return raw_sig

    def ecdsa_sign_recoverable(self, msg, raw=False, digest=hashlib.sha256):
        if not HAS_RECOVERABLE:
            raise Exception("secp256k1_recovery not enabled")

        msg32 = _hash32(msg, raw, digest)
        raw_sig = ffi.new('secp256k1_ecdsa_recoverable_signature *')

        signed = lib.secp256k1_ecdsa_sign_recoverable(
            self.ctx, raw_sig, msg32, self.private_key, ffi.NULL, ffi.NULL)
        assert signed == 1

        return raw_sig

    def schnorr_sign(self, msg, bip340tag, raw=False):
        if not HAS_SCHNORR:
            raise Exception("secp256k1_schnorr not enabled")

        msg_to_sign = self.bip340_tag(msg, raw, bip340tag)
        sig64 = ffi.new('char [64]')

        # FIXME: It's recommended to provide aux_rand32...
        signed = lib.secp256k1_schnorrsig_sign_custom(
            self.ctx, sig64, msg_to_sign, len(msg_to_sign),
            self.keypair, ffi.NULL)
        assert signed == 1

        return bytes(ffi.buffer(sig64, 64))


def _hash32(msg, raw, digest):
    if not raw:
        msg32 = digest(msg).digest()
    else:
        msg32 = msg
    if len(msg32) * 8 != 256:
        raise Exception("digest function must produce 256 bits")
    return msg32


def _gen_private_key():
    key = os.urandom(32)
    return key


def _tweak_public(inst, func, scalar):
    if not isinstance(scalar, bytes) or len(scalar) != 32:
        raise TypeError('scalar must be composed of 32 bytes')
    assert inst.public_key, "No public key defined."

    # Create a copy of the current public key.
    newpub = PublicKey(inst.serialize(), raw=True)

    res = func(inst.ctx, newpub.public_key, scalar)
    if not res:
        raise Exception("Tweak is out of range")

    return newpub


def _tweak_private(inst, func, scalar):
    if not isinstance(scalar, bytes) or len(scalar) != 32:
        raise TypeError('scalar must be composed of 32 bytes')

    # Create a copy of the current private key.
    key = ffi.new('char [32]', inst.private_key)

    res = func(inst.ctx, key, scalar)
    if not res:
        raise Exception("Tweak is out of range")

    return bytes(ffi.buffer(key, 32))


# Apparently flake8 thinks this is "too complex".  Maybe FIXME?
def _main_cli(args, out, encoding='utf-8'):  # noqa: C901
    import binascii

    def show_public(public_key):
        rawp = public_key.serialize()
        out.write(u"Public key: {}\n".format(
            binascii.hexlify(rawp).decode(encoding)))

    def sign(funcname, params):
        raw = bytes(bytearray.fromhex(params.private_key))
        priv = PrivateKey(raw)
        func = getattr(priv, funcname)
        sig = func(params.message)
        return priv, sig

    if args.action == 'privkey':
        if args.private_key:
            rawkey = bytes(bytearray.fromhex(args.private_key))
        else:
            rawkey = None
        priv = PrivateKey(rawkey)
        raw = priv.private_key
        out.write(u"{}\n".format(binascii.hexlify(raw).decode(encoding)))
        if args.show_pubkey:
            show_public(priv.pubkey)

    elif args.action == 'sign':
        priv, sig_raw = sign('ecdsa_sign', args)
        sig = priv.ecdsa_serialize(sig_raw)
        out.write(u"{}\n".format(binascii.hexlify(sig).decode(encoding)))
        if args.show_pubkey:
            show_public(priv.pubkey)

    elif args.action == 'checksig':
        raw = bytes(bytearray.fromhex(args.public_key))
        sig = bytes(bytearray.fromhex(args.signature))
        pub = PublicKey(raw, raw=True)
        try:
            sig_raw = pub.ecdsa_deserialize(sig)
            good = pub.ecdsa_verify(args.message, sig_raw)
        except:  # noqa: E722
            good = False
        out.write(u"{}\n".format(good))
        return 0 if good else 1

    elif args.action == 'signrec':
        priv, sig = sign('ecdsa_sign_recoverable', args)
        sig, recid = priv.ecdsa_recoverable_serialize(sig)
        out.write(u"{} {}\n".format(binascii.hexlify(sig).decode(encoding),
                                    recid))
        if args.show_pubkey:
            show_public(priv.pubkey)

    elif args.action == 'recpub':
        empty = PublicKey(flags=ALL_FLAGS)
        sig_raw = bytes(bytearray.fromhex(args.signature))
        sig = empty.ecdsa_recoverable_deserialize(sig_raw, args.recid)
        pubkey = empty.ecdsa_recover(args.message, sig)
        show_public(PublicKey(pubkey))

    return 0


def _parse_cli():
    import sys
    from argparse import ArgumentParser

    py2 = sys.version_info.major == 2
    enc = sys.getfilesystemencoding()

    def bytes_input(s):
        return s if py2 else s.encode(enc)

    parser = ArgumentParser(prog="secp256k1")
    subparser = parser.add_subparsers(dest='action')

    genparser = subparser.add_parser('privkey')
    genparser.add_argument('-p', '--show-pubkey', action='store_true')
    genparser.add_argument('-k', '--private_key')

    sign = subparser.add_parser('sign')
    sign.add_argument('-k', '--private-key', required=True)
    sign.add_argument('-m', '--message', required=True, type=bytes_input)
    sign.add_argument('-p', '--show-pubkey', action='store_true')

    signrec = subparser.add_parser('signrec')
    signrec.add_argument('-k', '--private-key', required=True)
    signrec.add_argument('-m', '--message', required=True, type=bytes_input)
    signrec.add_argument('-p', '--show-pubkey', action='store_true')

    check = subparser.add_parser('checksig')
    check.add_argument('-p', '--public-key', required=True)
    check.add_argument('-m', '--message', required=True, type=bytes_input)
    check.add_argument('-s', '--signature', required=True)

    recpub = subparser.add_parser('recpub')
    recpub.add_argument('-m', '--message', required=True, type=bytes_input)
    recpub.add_argument('-i', '--recid', required=True, type=int)
    recpub.add_argument('-s', '--signature', required=True)

    return parser, enc


def main():
    import sys
    parser, enc = _parse_cli()
    args = parser.parse_args(sys.argv[1:])
    sys.exit(_main_cli(args, sys.stdout, enc))


if __name__ == '__main__':
    main()
