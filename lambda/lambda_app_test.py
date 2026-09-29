import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lambda_app


class InventoryHelpersTest(unittest.TestCase):
    def test_fold_treats_yo_as_ye_and_collapses_space(self):
        self.assertEqual(lambda_app.fold("  Ёлка\nзеленая  "), "елка зеленая")

    def test_query_tokens_keep_number_and_russian_word(self):
        self.assertEqual(lambda_app.query_tokens("Коробка 12, провода"), ["коробка", "12", "провода"])

    def test_search_text_contains_folded_contents(self):
        text = lambda_app.build_search("12", "Ёлка", "Гирлянда и провод")
        self.assertIn("елка", text)
        self.assertIn("гирлянда", text)
        self.assertIn("12", text)

    def test_hidden_photo_text_is_searchable_and_folded(self):
        text = lambda_app.build_search("2", "", "наждачка", "Жёлтая Ёмкость")
        self.assertIn("наждачка", text)
        self.assertIn("емкость", text)
        self.assertNotIn("ё", text)

    def test_media_path_is_stable(self):
        self.assertEqual(lambda_app.signed_get("boxes/1/original/abc.jpg"), "/media/boxes/1/original/abc.jpg")
        self.assertIsNone(lambda_app.signed_get(""))

    def test_extract_response_text_reads_output_blocks(self):
        text = lambda_app.extract_response_text({
            "output": [{"content": [{"type": "output_text", "text": "рубанок и стамеска"}]}],
        })
        self.assertEqual(text, "рубанок и стамеска")

    def test_sort_key_orders_numbers_numerically(self):
        self.assertLess(lambda_app.sort_key("2"), lambda_app.sort_key("10"))
        self.assertLess(lambda_app.sort_key("10"), lambda_app.sort_key("A-1"))

    def test_number_validation(self):
        self.assertEqual(lambda_app.validate_number("A-12"), "A-12")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.validate_number("../12")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.validate_number("")

    def test_photo_key_must_stay_inside_the_box(self):
        good = "boxes/12/original/" + ("a" * 32) + ".jpg"
        self.assertTrue(lambda_app.key_allowed("12", good, "original"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/12/original/../99/" + ("a" * 32) + ".jpg", "original"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/99/original/" + ("a" * 32) + ".jpg", "original"))
        thumb = "boxes/12/thumb/" + ("b" * 32) + ".jpg"
        self.assertTrue(lambda_app.key_allowed("12", thumb, "thumb"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/12/thumb/" + ("b" * 32) + ".png", "thumb"))

    def test_cursor_roundtrip(self):
        key = {"pk": "BOX#12", "sk": "META"}
        self.assertEqual(lambda_app.decode_cursor(lambda_app.encode_cursor(key)), key)
        self.assertIsNone(lambda_app.decode_cursor(None))
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.decode_cursor("%%%")

    def test_preview_flattens_and_truncates(self):
        self.assertEqual(lambda_app.preview("кабель\nи лампа"), "кабель и лампа")
        self.assertTrue(lambda_app.preview("а" * 200).endswith("…"))

    def test_clean_text_limits(self):
        self.assertEqual(lambda_app.clean_text(None, 10, "Описание"), "")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.clean_text("слишком длинно", 5, "Описание")


def openssl_key(tmp, command, *args):
    key_path = os.path.join(tmp, "key.pem")
    subprocess.run(["openssl", command, "-out", key_path, *args], check=True, capture_output=True)
    return key_path


def claims_event(claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


class ActorTest(unittest.TestCase):
    def test_verified_email_names_the_user(self):
        event = claims_event({"email": "Person@Example.com", "email_verified": "true"})
        self.assertEqual(lambda_app.actor(event), "person@example.com")

    def test_unverified_or_missing_email_is_refused(self):
        for claims in ({"email": "person@example.com", "email_verified": "false"}, {"email": "person@example.com"}, {"sub": "abc"}):
            with self.assertRaises(lambda_app.ApiError) as caught:
                lambda_app.actor(claims_event(claims))
            self.assertEqual(caught.exception.status, 403)

    def test_settings_of_an_unverified_email_are_not_read(self):
        event = dict(claims_event({"email": "owner@example.com", "email_verified": "false"}), rawPath="/api/settings")
        event["requestContext"]["http"] = {"method": "GET"}

        class NoRead:
            def get_item(self, **kwargs):
                raise AssertionError("settings were read")

        lambda_app._table = NoRead()
        try:
            self.assertEqual(lambda_app.handler(event, None)["statusCode"], 403)
        finally:
            lambda_app._table = None


class LanguageTest(unittest.TestCase):
    def test_errors_follow_the_page_language(self):
        event = {"rawPath": "/api/nowhere", "headers": {"X-Inventory-Lang": "fr"}, "requestContext": {"http": {"method": "GET"}}}
        self.assertEqual(lambda_app.json.loads(lambda_app.handler(event, None)["body"])["error"], "Introuvable")

    def test_accept_language_and_russian_default(self):
        self.assertEqual(lambda_app.request_language({"headers": {"accept-language": "it-IT,it;q=0.9"}}), "it")
        self.assertEqual(lambda_app.request_language({"headers": {"accept-language": "de-DE"}}), "ru")

    def test_field_names_are_translated_too(self):
        with self.assertRaises(lambda_app.ApiError) as caught:
            lambda_app.clean_text("x" * 6, 5, "Описание")
        error = caught.exception
        self.assertEqual(lambda_app.translate("en", error.message, error.values), "Description is longer than 5 characters")
        self.assertEqual(lambda_app.translate("ru", error.message, error.values), "Описание длиннее 5 символов")

    def test_every_message_has_three_translations(self):
        self.assertTrue(all(len(value) == 3 for value in lambda_app.MESSAGES.values()))

    def test_query_tokens_keep_accented_words(self):
        self.assertEqual(lambda_app.query_tokens("Étagère, scatola 12"), ["étagère", "scatola", "12"])

    def test_recognition_prompt_per_language(self):
        self.assertEqual(set(lambda_app.RECOGNITION_PROMPTS), set(lambda_app.LANGUAGES))


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Pager:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kwargs):
        return [self.fn(**kwargs)]


class FakeCognito:
    def __init__(self):
        self.users = {}
        self.created = {}
        self.groups = {"admins": set()}

    def get_paginator(self, name):
        return Pager(getattr(self, name))

    def need(self, username):
        if username not in self.users:
            raise ClientError("UserNotFoundException")
        return self.users[username]

    def list_users(self, UserPoolId):
        return {"Users": [dict(user, Attributes=[{"Name": "email", "Value": email}]) for email, user in self.users.items()]}

    def list_users_in_group(self, UserPoolId, GroupName):
        return {"Users": [{"Username": email, "Attributes": [{"Name": "email", "Value": email}]} for email in self.groups[GroupName]]}

    def admin_get_user(self, UserPoolId, Username):
        return {"Username": Username, "UserMFASettingList": ["SOFTWARE_TOKEN_MFA"] if self.need(Username).get("mfa") else []}

    def admin_create_user(self, UserPoolId, Username, **kwargs):
        if Username in self.users:
            raise ClientError("UsernameExistsException")
        self.users[Username] = {"Username": Username, "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True,
                                "UserCreateDate": datetime.now(timezone.utc), "password": kwargs["TemporaryPassword"]}
        self.created[Username] = kwargs

    def admin_delete_user(self, UserPoolId, Username):
        self.need(Username)
        self.users.pop(Username)

    def admin_disable_user(self, UserPoolId, Username):
        self.need(Username)["Enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self.need(Username)["Enabled"] = True

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].add(Username)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].discard(Username)

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.need(Username).update(password=Password, UserStatus="FORCE_CHANGE_PASSWORD")

    def admin_set_user_mfa_preference(self, UserPoolId, Username, SoftwareTokenMfaSettings):
        self.need(Username)["mfa"] = SoftwareTokenMfaSettings["Enabled"]


class FakeTable:
    def __init__(self):
        self.items = {}

    def query(self, KeyConditionExpression, **kwargs):
        pk = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [{"pk": key[0], "sk": key[1]} for key in self.items if key[0] == pk]}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)


def call(method, path, body=None, params=None, email="person@example.com", groups=None):
    claims = {"email": email, "email_verified": "true"}
    if groups is not None:
        claims["cognito:groups"] = groups
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
    }
    result = lambda_app.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


class AdminTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", USER_POOL_ID="pool", ADMIN_GROUP="admins")
        self.cognito = lambda_app._cognito = FakeCognito()
        self.table = lambda_app._table = FakeTable()
        self.cognito.users["boss@example.com"] = {"Username": "boss@example.com", "UserStatus": "CONFIRMED", "Enabled": True,
                                                  "UserCreateDate": datetime.now(timezone.utc)}
        self.cognito.groups["admins"].add("boss@example.com")

    def tearDown(self):
        lambda_app._cognito = None
        lambda_app._table = None

    def admin(self, method, path, body=None, params=None):
        return call(method, path, body, params, email="boss@example.com", groups="[admins]")

    def users(self):
        return {user["email"]: user for user in self.admin("GET", "/api/admin/users")[1]["items"]}

    def test_only_admins_reach_admin_routes(self):
        self.assertEqual(call("GET", "/api/admin/users")[0], 403)
        self.assertEqual(call("GET", "/api/admin/users", groups="[readers]")[0], 403)
        self.assertEqual(self.admin("GET", "/api/admin/users")[0], 200)
        self.assertTrue(call("GET", "/api/me", email="boss@example.com", groups=["admins"])[1]["admin"])
        self.assertFalse(call("GET", "/api/me")[1]["admin"])

    def test_invite_update_and_delete_user(self):
        status, created = self.admin("POST", "/api/admin/users", {"email": " New@Example.com ", "admin": True})
        self.assertEqual(status, 200)
        self.assertEqual(created["email"], "new@example.com")
        password = created["temporary_password"]
        self.assertTrue(len(password) >= 12 and any(c.isupper() for c in password) and any(c in "!@#%&*-_=+" for c in password))
        self.assertEqual(self.admin("POST", "/api/admin/users", {"email": "new@example.com"})[0], 409)
        self.assertEqual(self.admin("POST", "/api/admin/users", {"email": "not-an-email"})[0], 400)
        self.assertTrue(self.users()["new@example.com"]["admin"])
        self.assertTrue(self.users()["boss@example.com"]["self"])

        self.admin("PUT", "/api/admin/users", {"email": "new@example.com", "enabled": False, "admin": False})
        self.assertFalse(self.users()["new@example.com"]["enabled"])
        self.assertFalse(self.users()["new@example.com"]["admin"])
        status, reset = self.admin("PUT", "/api/admin/users", {"email": "new@example.com", "reset_password": True})
        self.assertEqual(self.cognito.users["new@example.com"]["password"], reset["temporary_password"])

        self.table.items[("USER#new@example.com", "SETTINGS")] = {}
        self.table.items[("BOX#1", "META")] = {}
        self.assertEqual(self.admin("DELETE", "/api/admin/users", params={"email": "new@example.com"})[0], 200)
        self.assertNotIn("new@example.com", self.cognito.users)
        self.assertEqual(list(self.table.items), [("BOX#1", "META")], "settings go, boxes stay")

    def test_mfa_status_and_reset(self):
        self.cognito.users["phone@example.com"] = {"Username": "phone@example.com", "UserStatus": "CONFIRMED", "Enabled": True,
                                                   "UserCreateDate": datetime.now(timezone.utc), "mfa": True}
        self.assertTrue(self.users()["phone@example.com"]["mfa"])
        self.assertFalse(self.users()["boss@example.com"]["mfa"])
        self.assertEqual(call("PUT", "/api/admin/users", {"email": "phone@example.com", "reset_mfa": True})[0], 403)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "phone@example.com", "reset_mfa": True})[0], 200)
        self.assertFalse(self.cognito.users["phone@example.com"]["mfa"])

    def test_unknown_user_is_404(self):
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "nobody@example.com", "enabled": False})[0], 404)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "nobody@example.com", "reset_mfa": True})[0], 404)

    def test_demo_invite_sends_no_mail(self):
        os.environ["INVITE_EMAILS"] = "false"
        try:
            status, created = self.admin("POST", "/api/admin/users", {"email": "guest@example.com"})
        finally:
            os.environ["INVITE_EMAILS"] = "true"
        self.assertEqual(status, 200)
        self.assertFalse(created["emailed"])
        self.assertEqual(self.cognito.users["guest@example.com"]["password"], created["temporary_password"])
        self.assertEqual(self.cognito.created["guest@example.com"].get("MessageAction"), "SUPPRESS")

    def test_invite_emails_when_turned_on(self):
        os.environ["INVITE_EMAILS"] = "true"
        status, created = self.admin("POST", "/api/admin/users", {"email": "mail@example.com"})
        self.assertTrue(created["emailed"])
        self.assertEqual(self.cognito.created["mail@example.com"].get("DesiredDeliveryMediums"), ["EMAIL"])

    def test_admin_cannot_lock_themselves_out(self):
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "boss@example.com", "enabled": False})[0], 400)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "boss@example.com", "admin": False})[0], 400)
        self.assertEqual(self.admin("DELETE", "/api/admin/users", params={"email": "boss@example.com"})[0], 400)


class RecognitionSwitchTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", RECOGNITION="false")

        class NoRead:
            def get_item(self, **kwargs):
                raise AssertionError("settings were read")

        lambda_app._table = NoRead()

    def tearDown(self):
        os.environ["RECOGNITION"] = "true"
        lambda_app._table = None

    def test_settings_do_not_hand_out_keys_when_off(self):
        status, body = call("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"recognition": False, "grok_api_key": "", "grok_model": ""})

    def test_a_key_cannot_be_saved_when_off(self):
        self.assertEqual(call("PUT", "/api/settings", {"grok_api_key": "xai-secret", "grok_model": "grok-4.7"})[0], 403)

    def test_uploads_do_not_queue_recognition_when_off(self):
        self.assertFalse(lambda_app.recognition_configured("person@example.com"))


class SigningTest(unittest.TestCase):
    def test_signature_matches_openssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = openssl_key(tmp, "genrsa", "-traditional", "2048")
            with open(key_path) as handle:
                pem = handle.read()
            message = b'{"Statement":[]}'
            expected = subprocess.run(
                ["openssl", "dgst", "-sha1", "-sign", key_path],
                input=message, check=True, capture_output=True,
            ).stdout
            self.assertEqual(lambda_app.rsa_sha1_sign(message, lambda_app.load_rsa_private_key(pem)), expected)

    def test_pkcs8_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(openssl_key(tmp, "genpkey", "-algorithm", "RSA")) as handle:
                key = lambda_app.load_rsa_private_key(handle.read())
            self.assertEqual(key["p"] * key["q"], key["n"])

    def test_cookies_cover_media_in_cloudfront_alphabet(self):
        os.environ["CLOUDFRONT_KEY_PAIR_ID"] = "K123"
        with tempfile.TemporaryDirectory() as tmp:
            with open(openssl_key(tmp, "genrsa", "-traditional", "2048")) as handle:
                lambda_app._signing_key = lambda_app.load_rsa_private_key(handle.read())
        cookies = lambda_app.media_cookies("boxes.example.com")
        policy = cookies[0].split(";")[0].split("=", 1)[1]
        self.assertNotRegex(policy, r"[+=/]")
        decoded = base64.b64decode(policy.replace("-", "+").replace("_", "=").replace("~", "/"))
        self.assertIn(b"https://boxes.example.com/media/*", decoded)
        self.assertTrue(all("Path=/media" in cookie for cookie in cookies))
        self.assertTrue(cookies[2].startswith("CloudFront-Key-Pair-Id=K123;"))


if __name__ == "__main__":
    unittest.main()
