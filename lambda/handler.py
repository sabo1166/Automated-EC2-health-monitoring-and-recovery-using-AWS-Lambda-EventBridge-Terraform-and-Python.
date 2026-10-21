"""Monitor the website and replace an unhealthy EC2 instance."""

import os
import time
import uuid
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

ec2 = boto3.client("ec2")
dynamodb = boto3.client("dynamodb")

PROJECT_NAME = os.environ["PROJECT_NAME"]
LOCK_TABLE = os.environ["LOCK_TABLE"]
HEALTH_CHECK_PATH = os.environ.get("HEALTH_CHECK_PATH", "/")
LOCK_LEASE_SECONDS = int(os.environ.get("LOCK_LEASE_SECONDS", "360"))


def acquire_lock(token):
    """Prevent concurrent replacement attempts."""
    now = int(time.time())

    try:
        dynamodb.put_item(
            TableName=LOCK_TABLE,
            Item={
                "LockId": {"S": "replacement"},
                "OwnerToken": {"S": token},
                "ExpiresAt": {"N": str(now + LOCK_LEASE_SECONDS)},
            },
            ConditionExpression=(
                "attribute_not_exists(LockId) OR ExpiresAt < :now"
            ),
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_lock(token):
    """Release only the lock owned by this invocation."""
    try:
        dynamodb.delete_item(
            TableName=LOCK_TABLE,
            Key={"LockId": {"S": "replacement"}},
            ConditionExpression="OwnerToken = :token",
            ExpressionAttributeValues={":token": {"S": token}},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def find_managed_instances():
    """Find active project instances, newest first."""
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [PROJECT_NAME]},
            {"Name": "tag:ManagedBy", "Values": ["SelfHealing"]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )

    instances = [
        instance
        for reservation in response["Reservations"]
        for instance in reservation["Instances"]
    ]

    return sorted(
        instances,
        key=lambda instance: instance["LaunchTime"],
        reverse=True,
    )


def website_is_healthy(public_ip):
    """Return True when the website responds successfully over HTTP."""
    if not public_ip:
        return False

    url = f"http://{public_ip}{HEALTH_CHECK_PATH}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SelfHealingHealthCheck/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def wait_for_website(instance_id):
    """Wait for EC2 health checks, then verify the website."""
    ec2.get_waiter("instance_status_ok").wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 10, "MaxAttempts": 15},
    )

    response = ec2.describe_instances(InstanceIds=[instance_id])
    instance = response["Reservations"][0]["Instances"][0]
    public_ip = instance.get("PublicIpAddress")

    for attempt in range(12):
        if website_is_healthy(public_ip):
            return True

        if attempt < 11:
            time.sleep(5)

    return False


def launch_replacement():
    """Launch a replacement with the configured network and website."""
    response = ec2.run_instances(
        ImageId=os.environ["AMI_ID"],
        InstanceType=os.environ["INSTANCE_TYPE"],
        MinCount=1,
        MaxCount=1,
        SubnetId=os.environ["SUBNET_ID"],
        SecurityGroupIds=[os.environ["SECURITY_GROUP_ID"]],
        AssociatePublicIpAddress=True,
        UserData=os.environ["USER_DATA"],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Project", "Value": PROJECT_NAME},
                    {"Key": "ManagedBy", "Value": "SelfHealing"},
                    {"Key": "Role", "Value": "primary"},
                    {"Key": "Name", "Value": f"{PROJECT_NAME}-primary"},
                ],
            }
        ],
    )

    return response["Instances"][0]["InstanceId"]


def lambda_handler(event, context):
    """Replace an unhealthy instance only after its replacement is verified."""
    del event, context

    token = str(uuid.uuid4())

    if not acquire_lock(token):
        return {
            "status": "skipped",
            "reason": "Another health check holds the replacement lock.",
        }

    replacement_id = None

    try:
        instances = find_managed_instances()
        current = instances[0] if instances else None
        current_id = current["InstanceId"] if current else None

        if current:
            state = current["State"]["Name"]

            if state in ("pending", "stopping"):
                return {
                    "status": "waiting",
                    "instance_id": current_id,
                    "state": state,
                }

            if state == "running" and website_is_healthy(
                current.get("PublicIpAddress")
            ):
                return {
                    "status": "healthy",
                    "instance_id": current_id,
                }

        # Preserve the existing instance until the replacement is healthy.
        replacement_id = launch_replacement()

        try:
            ec2.get_waiter("instance_running").wait(
                InstanceIds=[replacement_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": 24},
            )

            if not wait_for_website(replacement_id):
                raise RuntimeError(
                    "Replacement failed the website health check."
                )

        except Exception:
            # Do not delete the old instance if recovery fails.
            ec2.terminate_instances(InstanceIds=[replacement_id])
            raise

        if current_id and current_id != replacement_id:
            ec2.terminate_instances(InstanceIds=[current_id])

        return {
            "status": "replaced",
            "old_instance_id": current_id,
            "new_instance_id": replacement_id,
        }

    finally:
        release_lock(token)
