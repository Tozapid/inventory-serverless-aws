import json
import os
import unittest

import reset

SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed")


class FakeDynamo:
    def __init__(self, items):
        self.items = {(i["pk"]["S"], i["sk"]["S"]): i for i in items}

    def scan(self, TableName, ProjectionExpression, ExclusiveStartKey=None):
        keys = sorted(self.items)
        # Like DynamoDB, carry on after the last key even if it was deleted meanwhile.
        start = sum(1 for key in keys if ExclusiveStartKey and key <= ExclusiveStartKey)
        page = keys[start:start + 30]
        answer = {"Items": [{"pk": {"S": k[0]}, "sk": {"S": k[1]}} for k in page]}
        if start + 30 < len(keys):
            answer["LastEvaluatedKey"] = page[-1]
        return answer

    def batch_write_item(self, RequestItems):
        (requests,) = RequestItems.values()
        assert len(requests) <= 25
        for request in requests:
            if "PutRequest" in request:
                item = request["PutRequest"]["Item"]
                self.items[(item["pk"]["S"], item["sk"]["S"])] = item
            else:
                key = request["DeleteRequest"]["Key"]
                self.items.pop((key["pk"]["S"], key["sk"]["S"]))
        return {}


class Pager:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages()


class FakeS3:
    def __init__(self):
        self.versions = [{"Key": f"boxes/{n}/original/a.jpg", "VersionId": f"v{n}"} for n in range(1500)]
        self.markers = [{"Key": "boxes/x/thumb/b.jpg", "VersionId": "m1"}]
        self.seed = [{"Key": "seed/photos/hand-tools.jpg", "VersionId": "s1"}]

    def get_paginator(self, name):
        return Pager(lambda: [{"Versions": list(self.versions) + list(self.seed), "DeleteMarkers": list(self.markers)}])

    def delete_objects(self, Bucket, Delete):
        assert len(Delete["Objects"]) <= 1000
        gone = {(o["Key"], o["VersionId"]) for o in Delete["Objects"]}
        self.versions = [v for v in self.versions if (v["Key"], v["VersionId"]) not in gone]
        self.markers = [m for m in self.markers if (m["Key"], m["VersionId"]) not in gone]
        self.seed = [s for s in self.seed if (s["Key"], s["VersionId"]) not in gone]


class FakeCognito:
    def __init__(self, users):
        self.users = dict(users)
        self.calls = []

    def get_paginator(self, name):
        return Pager(lambda: [{"Users": [
            {"Username": u, "Attributes": [{"Name": "email", "Value": e}]} for u, e in self.users.items()
        ]}])

    def admin_delete_user(self, UserPoolId, Username):
        del self.users[Username]

    def admin_create_user(self, UserPoolId, Username, UserAttributes, MessageAction):
        self.users[Username] = Username
        self.calls.append(("create", Username, MessageAction))

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("password", kwargs["Username"], kwargs["Password"], kwargs["Permanent"]))

    def admin_enable_user(self, **kwargs):
        self.calls.append(("enable", kwargs["Username"]))

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("group", kwargs["Username"], kwargs["GroupName"]))

    def admin_set_user_mfa_preference(self, **kwargs):
        self.calls.append(("mfa", kwargs["Username"], kwargs["SoftwareTokenMfaSettings"]["Enabled"]))

    def admin_user_global_sign_out(self, **kwargs):
        self.calls.append(("sign_out", kwargs["Username"]))


class FakeSsm:
    def get_parameter(self, Name, WithDecryption):
        return {"Parameter": {"Value": "admin123"}}


class ResetTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media", USER_POOL_ID="pool",
                          ADMIN_EMAIL="admin@example.com", ADMIN_PASSWORD_PARAMETER="/inventory/admin-password", ADMIN_GROUP="admins")
        items = [{"pk": {"S": f"BOX#{n}"}, "sk": {"S": "META"}} for n in range(70)]
        items += [{"pk": {"S": "USER#guest@example.com"}, "sk": {"S": "SETTINGS"}}]
        self.ddb, self.s3, self.ssm = FakeDynamo(items), FakeS3(), FakeSsm()
        self.cognito = FakeCognito({"uuid-admin": "Admin@Example.com", "uuid-guest": "guest@example.com"})
        reset._clients.update({"dynamodb": self.ddb, "s3": self.s3, "cognito-idp": self.cognito, "ssm": self.ssm})
        reset.SEED_FILE = os.path.join(SEED, "boxes.json")
        with open(reset.SEED_FILE, encoding="utf-8") as source:
            self.seed = json.load(source)["boxes"]

    def value(self, item, name):
        return next(iter(item[name].values()))

    def test_wipes_data_photos_and_other_users(self):
        result = reset.handler({}, None)
        self.assertEqual(result["items"], 71)
        self.assertFalse([key for key in self.ddb.items if key == ("BOX#0", "META") or key[0].startswith("USER#")])
        self.assertEqual(result["files"], 1501)
        self.assertEqual((self.s3.versions, self.s3.markers), ([], []))
        self.assertEqual(self.s3.seed, [{"Key": "seed/photos/hand-tools.jpg", "VersionId": "s1"}])
        self.assertEqual(self.cognito.users, {"uuid-admin": "Admin@Example.com"})
        self.assertEqual(result["users"], {"removed": 1, "admin_recreated": False})
        self.assertIn(("password", "admin@example.com", "admin123", True), self.cognito.calls)
        self.assertIn(("mfa", "admin@example.com", False), self.cognito.calls)
        self.assertIn(("group", "admin@example.com", "admins"), self.cognito.calls)
        self.assertEqual(self.cognito.calls[-1], ("sign_out", "admin@example.com"))

    def test_recreates_a_deleted_admin(self):
        self.cognito.users = {"uuid-guest": "guest@example.com"}
        result = reset.handler({}, None)
        self.assertTrue(result["users"]["admin_recreated"])
        self.assertIn(("create", "admin@example.com", "SUPPRESS"), self.cognito.calls)

    def test_writes_the_example_boxes(self):
        result = reset.handler({}, None)
        self.assertEqual(result["seeded"], len(self.seed))
        meta = {k[0]: v for k, v in self.ddb.items.items() if k[1] == "META"}
        self.assertEqual(sorted(meta), sorted("BOX#" + b["number"] for b in self.seed))
        history = [k for k in self.ddb.items if k[1].startswith("HIST#")]
        self.assertEqual(len(history), sum(len(b["history"]) for b in self.seed))
        tools = meta["BOX#2"]
        self.assertEqual(self.value(tools, "gsi1pk"), "BOX")
        self.assertEqual(self.value(tools, "gsi1sk"), "n:000000000002")
        self.assertEqual(self.value(tools, "photo_key"), "seed/photos/hand-tools.jpg")
        self.assertEqual(self.value(tools, "updated_by"), "admin@example.com")
        self.assertIn("pliers", self.value(tools, "search_text"), "the hidden text is searchable")
        self.assertEqual(self.value(meta["BOX#A-7"], "gsi1sk"), "s:a-7")
        self.assertNotIn("photo_key", meta["BOX#5"])

    def test_example_photos_exist(self):
        photos = os.listdir(os.path.join(SEED, "photos"))
        for box in self.seed:
            if box["photo"]:
                self.assertIn(box["photo"] + ".jpg", photos)
                self.assertIn(box["photo"] + "-thumb.jpg", photos)

    def test_history_matches_the_card(self):
        for box in self.seed:
            if box["history"]:
                self.assertEqual(box["history"][0]["before"], "", box["number"])
                self.assertEqual(box["history"][-1]["after"], box["contents"], box["number"])

    def test_no_seed_file_means_no_examples(self):
        reset.SEED_FILE = os.path.join(SEED, "missing.json")
        result = reset.handler({}, None)
        self.assertEqual(result["seeded"], 0)
        self.assertEqual(self.ddb.items, {})

    def test_empty_site_is_fine(self):
        self.ddb.items, self.s3.versions, self.s3.markers = {}, [], []
        self.cognito.users = {"uuid-admin": "admin@example.com"}
        result = reset.handler({}, None)
        self.assertEqual((result["items"], result["files"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
