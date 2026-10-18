import json
import struct
import unittest
from pathlib import Path

import link_protocol as lp

VECTORS = Path(__file__).resolve().parent / "fixtures" / "link-vectors.json"


def load_vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


class PairingPayloadTests(unittest.TestCase):
    def setUp(self):
        self.vectors = load_vectors()

    def test_encodes_every_vector_exactly(self):
        for case in self.vectors["payloads"]:
            with self.subTest(case["name"]):
                offer = lp.PairingOffer(
                    computer_id=bytes.fromhex(case["computer_id"]),
                    secret=bytes.fromhex(case["secret"]),
                    expires_at=case["expires_at"],
                    name=case["computer_name"],
                    os=case["os"],
                    relay_url=case["relay_url"],
                    addresses=tuple(case["addresses"]),
                )
                self.assertEqual(offer.encode(), case["text"])
                self.assertEqual(offer.safety_code(), case["safety_code"])

    def test_decodes_every_vector_to_its_fields(self):
        for case in self.vectors["payloads"]:
            with self.subTest(case["name"]):
                offer = lp.PairingOffer.decode(case["text"])
                self.assertEqual(offer.computer_id.hex(), case["computer_id"])
                self.assertEqual(offer.secret.hex(), case["secret"])
                self.assertEqual(offer.expires_at, case["expires_at"])
                self.assertEqual(offer.name, case["computer_name"])
                self.assertEqual(offer.os, case["os"])
                self.assertEqual(offer.relay_url, case["relay_url"])
                self.assertEqual(list(offer.addresses), case["addresses"])

    def test_pasted_text_tolerates_line_breaks_and_lowercase(self):
        text = self.vectors["payloads"][0]["text"]
        mangled = text[:30].lower() + "\n  " + text[30:]
        self.assertEqual(lp.PairingOffer.decode(mangled), lp.PairingOffer.decode(text))

    def test_rejects_every_invalid_vector(self):
        for case in self.vectors["invalid_payloads"]:
            with self.subTest(case["name"]):
                with self.assertRaises(lp.PayloadError):
                    lp.PairingOffer.decode(case["text"])

    def test_payload_fits_alphanumeric_qr_mode(self):
        allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")
        for case in self.vectors["payloads"]:
            self.assertTrue(set(case["text"]) <= allowed, case["name"])

    def test_long_names_are_cut_on_a_character_boundary(self):
        offer = lp.PairingOffer(computer_id=bytes(32), secret=bytes(range(32)), expires_at=10, name="Ordenador de Ana Pérez García " * 4)
        decoded = lp.PairingOffer.decode(offer.encode())
        self.assertLessEqual(len(decoded.name.encode("utf-8")), lp.MAX_NAME_BYTES)
        self.assertTrue(("Ordenador de Ana Pérez García " * 4).startswith(decoded.name))

    def test_expiry(self):
        offer = lp.PairingOffer.decode(self.vectors["payloads"][0]["text"])
        self.assertFalse(offer.is_expired(offer.expires_at - 1))
        self.assertTrue(offer.is_expired(offer.expires_at))


class PairingProofTests(unittest.TestCase):
    def test_proofs_and_confirmations_match_vectors(self):
        for case in load_vectors()["proofs"]:
            with self.subTest(case["phone_name"]):
                secret = bytes.fromhex(case["secret"])
                computer = bytes.fromhex(case["computer_id"])
                phone = bytes.fromhex(case["phone_id"])
                self.assertEqual(lp.pairing_proof(secret, computer, phone, case["phone_name"]).hex(), case["proof"])
                self.assertEqual(lp.pairing_confirmation(secret, computer, phone).hex(), case["confirmation"])

    def test_a_proof_binds_both_identities_and_the_secret(self):
        case = load_vectors()["proofs"][0]
        secret = bytes.fromhex(case["secret"])
        computer = bytes.fromhex(case["computer_id"])
        phone = bytes.fromhex(case["phone_id"])
        proof = lp.pairing_proof(secret, computer, phone, case["phone_name"])
        self.assertNotEqual(proof, lp.pairing_proof(secret, computer, bytes(32), case["phone_name"]))
        self.assertNotEqual(proof, lp.pairing_proof(secret, bytes(32), phone, case["phone_name"]))
        self.assertNotEqual(proof, lp.pairing_proof(bytes(32), computer, phone, case["phone_name"]))
        self.assertNotEqual(proof, lp.pairing_proof(secret, computer, phone, case["phone_name"] + "2"))
        self.assertNotEqual(proof, lp.pairing_confirmation(secret, computer, phone))


class FrameTests(unittest.TestCase):
    def test_vectors_decode_to_their_messages(self):
        for case in load_vectors()["frames"]:
            reader = lp.FrameReader()
            self.assertEqual(reader.feed(bytes.fromhex(case["hex"])), [case["message"]])
            self.assertEqual(reader.pending, 0)

    def test_encoding_round_trips_with_a_length_header(self):
        for case in load_vectors()["frames"]:
            frame = lp.encode_frame(case["message"])
            self.assertEqual(struct.unpack(">I", frame[:4])[0], len(frame) - 4)
            self.assertEqual(lp.FrameReader().feed(frame), [case["message"]])

    def test_frames_split_and_joined_arbitrarily(self):
        joined = b"".join(bytes.fromhex(case["hex"]) for case in load_vectors()["frames"])
        reader = lp.FrameReader()
        received = []
        for index in range(0, len(joined), 7):
            received += reader.feed(joined[index:index + 7])
        self.assertEqual(received, [case["message"] for case in load_vectors()["frames"]])

    def test_oversized_header_is_refused_before_reading_the_body(self):
        with self.assertRaises(lp.FrameError):
            lp.FrameReader().feed(bytes.fromhex(load_vectors()["oversized_frame_header"]))

    def test_non_object_json_is_refused(self):
        body = b"[1,2]"
        with self.assertRaises(lp.FrameError):
            lp.FrameReader().feed(struct.pack(">I", len(body)) + body)


class RpcEnvelopeTests(unittest.TestCase):
    def test_result_and_remote_error(self):
        result, as_of = lp.rpc_result({"v": 1, "id": 3, "ok": True, "as_of": "2026-10-10T21:00:00+02:00", "result": {"days": []}}, 3)
        self.assertEqual(result, {"days": []})
        self.assertEqual(as_of, "2026-10-10T21:00:00+02:00")
        with self.assertRaises(lp.RemoteError) as raised:
            lp.rpc_result({"v": 1, "id": 4, "ok": False, "error": {"code": "invalid_params", "message": "Falta la fecha"}}, 4)
        self.assertEqual(raised.exception.code, "invalid_params")
        with self.assertRaises(lp.FrameError):
            lp.rpc_result({"v": 1, "id": 5, "ok": True, "result": {}}, 6)


if __name__ == "__main__":
    unittest.main()
