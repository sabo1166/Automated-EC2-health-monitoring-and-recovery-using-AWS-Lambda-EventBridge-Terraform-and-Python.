import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ParamValidationError
from botocore.validate import validate_parameters

HANDLER_PATH = Path(__file__).resolve().parents[1] / "lambda" / "handler.py"

ENV = {
    "PROJECT_NAME": "self-healing-ec2",
    "LOCK_TABLE": "lock-table",
    "AMI_ID": "ami-12345678",
    "INSTANCE_TYPE": "t3.micro",
    "SUBNET_ID": "subnet-12345678",
    "SECURITY_GROUP_ID": "sg-12345678",
    "USER_DATA": "#!/bin/bash\necho hello\n",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def load_handler():
    spec = importlib.util.spec_from_file_location("handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, ENV), mock.patch("boto3.client"):
        spec.loader.exec_module(module)
    return module


class LaunchReplacementTest(unittest.TestCase):
    def test_run_instances_arguments(self):
        handler = load_handler()
        handler.ec2 = mock.Mock()
        handler.ec2.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-0abc"}]
        }

        with mock.patch.dict(os.environ, ENV):
            instance_id = handler.launch_replacement()

        self.assertEqual(instance_id, "i-0abc")
        kwargs = handler.ec2.run_instances.call_args.kwargs

        # Invalid or conflicting top-level parameters must not be present.
        self.assertNotIn("AssociatePublicIpAddress", kwargs)
        self.assertNotIn("SubnetId", kwargs)
        self.assertNotIn("SecurityGroupIds", kwargs)

        self.assertEqual(
            kwargs["NetworkInterfaces"],
            [
                {
                    "DeviceIndex": 0,
                    "SubnetId": "subnet-12345678",
                    "Groups": ["sg-12345678"],
                    "AssociatePublicIpAddress": True,
                }
            ],
        )
        self.assertEqual(kwargs["ImageId"], "ami-12345678")
        self.assertEqual(kwargs["InstanceType"], "t3.micro")
        self.assertEqual(kwargs["UserData"], ENV["USER_DATA"])
        self.assertEqual((kwargs["MinCount"], kwargs["MaxCount"]), (1, 1))

        tags = {
            t["Key"]: t["Value"]
            for t in kwargs["TagSpecifications"][0]["Tags"]
        }
        self.assertEqual(kwargs["TagSpecifications"][0]["ResourceType"], "instance")
        self.assertEqual(
            tags,
            {
                "Project": "self-healing-ec2",
                "ManagedBy": "SelfHealing",
                "Role": "primary",
                "Name": "self-healing-ec2-primary",
            },
        )

    def test_params_pass_botocore_validation(self):
        """Validate the real request shape against botocore's EC2 model."""
        import boto3

        handler = load_handler()
        client = boto3.client(
            "ec2",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="x",
        )
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {"Instances": [{"InstanceId": "i-0abc"}]}

        handler.ec2 = mock.Mock()
        handler.ec2.run_instances.side_effect = fake_run
        with mock.patch.dict(os.environ, ENV):
            handler.launch_replacement()

        # Raises ParamValidationError if the shape is wrong; no network call.
        model = client.meta.service_model.operation_model("RunInstances")
        validate_parameters(captured, model.input_shape)

        # Sanity check: the old, invalid shape is rejected by the same model.
        bad = dict(captured, AssociatePublicIpAddress=True)
        with self.assertRaises(ParamValidationError):
            validate_parameters(bad, model.input_shape)


if __name__ == "__main__":
    unittest.main()
