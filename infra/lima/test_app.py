import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import adapt, decode_documents, guarded_cluster, translate


class CapacityDeploymentTests(unittest.TestCase):
    def test_addresses_are_translated_without_mutating_source(self):
        source = {"addresses": ["172.22.0.2"], "cidr": "172.22.0.5/32"}
        self.assertEqual(
            translate(source), {"addresses": ["172.28.0.2"], "cidr": "172.28.0.5/32"}
        )
        self.assertEqual(source["addresses"], ["172.22.0.2"])

    def test_json_stream_and_list(self):
        first = {"kind": "Namespace", "metadata": {"name": "x"}}
        second = {"kind": "Service", "metadata": {"name": "y"}}
        stream = (
            json.dumps(first) + "\n" + json.dumps({"kind": "List", "items": [second]})
        )
        self.assertEqual(decode_documents(stream), [first, second])

    def test_malformed_render_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            decode_documents('{"kind":"Namespace"} broken')

    def test_api_disables_external_and_selects_capacity_image(self):
        items = [
            {
                "kind": "Deployment",
                "metadata": {"name": "chat"},
                "spec": {
                    "replicas": 0,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "image": "laughtale-chat:external-lab-v8",
                                    "resources": {},
                                    "env": [
                                        {"name": "EXTERNAL_ENABLED", "value": "true"}
                                    ],
                                }
                            ]
                        }
                    },
                },
            }
        ]
        output = adapt(items)[0]
        container = output["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(output["spec"]["replicas"], 1)
        self.assertEqual(container["image"], "laughtale-chat:capacity-v1")
        self.assertEqual(container["env"][0]["value"], "false")
        self.assertEqual(items[0]["spec"]["replicas"], 0)

    def test_wrong_api_is_rejected_before_cluster_access(self):
        with patch(
            "app.run", return_value=SimpleNamespace(stdout="https://127.0.0.1:6443")
        ) as command:
            with self.assertRaises(RuntimeError):
                guarded_cluster()
            self.assertEqual(command.call_count, 1)

    def test_matching_name_but_wrong_cluster_is_rejected(self):
        with (
            patch(
                "app.run",
                side_effect=[
                    SimpleNamespace(stdout=value)
                    for value in (
                        "https://127.0.0.1:26447",
                        "other-uid",
                        "capacity-uid",
                    )
                ],
            ),
            self.assertRaises(RuntimeError),
        ):
            guarded_cluster()

    def test_matching_cluster_is_accepted(self):
        with patch(
            "app.run",
            side_effect=[
                SimpleNamespace(stdout=value)
                for value in ("https://127.0.0.1:26447", "capacity-uid", "capacity-uid")
            ],
        ):
            guarded_cluster()


if __name__ == "__main__":
    unittest.main()
