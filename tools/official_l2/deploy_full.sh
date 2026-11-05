#!/usr/bin/env bash
# Run only in the explicitly approved CloudShell session for this full.
set -Eeuo pipefail

readonly EXPECTED_ACCOUNT=555066174629
readonly REGION=us-east-1
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMPLATE="$SCRIPT_DIR/infra_full.yaml"
readonly ACTION="${1:-}"
readonly STACK_NAME="${2:-}"

if [[ "$ACTION" != deploy && "$ACTION" != fetch && "$ACTION" != cleanup ]]; then
  echo 'Usage: deploy_full.sh deploy STACK_NAME BUNDLE.zip | fetch STACK_NAME OUTPUT_DIR | cleanup STACK_NAME VERIFIED_OUTPUT_DIR' >&2
  exit 2
fi
if [[ ! "$STACK_NAME" =~ ^csv2-l2-full-[a-zA-Z0-9-]+$ ]]; then
  echo 'Stack name must begin with csv2-l2-full-.' >&2
  exit 2
fi
if [[ "${HLP_APPROVED_CLOUD_SESSION:-}" != "$EXPECTED_ACCOUNT" ]]; then
  echo 'Set HLP_APPROVED_CLOUD_SESSION=555066174629 only after this exact full is approved.' >&2
  exit 2
fi
if [[ -z "${AWS_CONTAINER_CREDENTIALS_FULL_URI:-}" && -z "${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}" ]]; then
  echo 'This launcher requires the approved CloudShell container credential session.' >&2
  exit 2
fi
# Use only the active CloudShell session credential provider, never local profiles.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" AWS_STS_REGIONAL_ENDPOINTS=regional
export AWS_MAX_ATTEMPTS=1
export AWS_PAGER=''

verify_identity() {
  local account
  account=$(aws sts get-caller-identity --region "$REGION" --cli-connect-timeout 8 --cli-read-timeout 30 --query Account --output text)
  if [[ "$account" != "$EXPECTED_ACCOUNT" ]]; then
    echo 'CloudShell identity differs from approved account. Stopping.' >&2
    exit 1
  fi
}

run_aws() {
  verify_identity
  aws --region "$REGION" --cli-connect-timeout 8 --cli-read-timeout 30 "$@"
}

stack_description() {
  run_aws cloudformation describe-stacks --stack-name "$STACK_NAME" --output json
}

stack_output() {
  local description="$1" name="$2"
  python3 -c 'import json,sys; d=json.load(sys.stdin); name=sys.argv[1]; print(next(v["OutputValue"] for v in d["Stacks"][0]["Outputs"] if v["OutputKey"]==name))' "$name" <<< "$description"
}

wait_stack() {
  local expected="$1" started=$SECONDS state
  while (( SECONDS - started < 900 )); do
    state=$(run_aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text)
    if [[ "$state" == "$expected" ]]; then return; fi
    if [[ "$state" == *FAILED* || "$state" == *ROLLBACK* ]]; then
      echo "CloudFormation ended in $state." >&2
      return 1
    fi
    sleep 10
  done
  echo 'CloudFormation operation exceeded the 15-minute deadline.' >&2
  return 1
}

wait_stack_deleted() {
  local started=$SECONDS result state
  while (( SECONDS - started < 900 )); do
    if result=$(stack_description 2>&1); then
      state=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Stacks"][0]["StackStatus"])' <<< "$result")
      if [[ "$state" == DELETE_FAILED ]]; then
        echo 'Stack cleanup failed. Inspect this exact stack before retrying.' >&2
        return 1
      fi
    elif [[ "$result" == *ValidationError* && "$result" == *'does not exist'* ]]; then
      return
    else
      echo 'Could not verify the full stack deletion state.' >&2
      return 1
    fi
    sleep 10
  done
  echo 'Stack cleanup exceeded the 15-minute deadline.' >&2
  return 1
}

terminate_own_worker() {
  local instance_id
  instance_id=$(run_aws ec2 describe-instances --filters "Name=tag:aws:cloudformation:stack-name,Values=$STACK_NAME" 'Name=instance-state-name,Values=pending,running,stopping,stopped' --query 'Reservations[].Instances[].InstanceId' --output text)
  if [[ -n "$instance_id" && "$instance_id" != None ]]; then
    if [[ ! "$instance_id" =~ ^i-[0-9a-f]+$ ]]; then
      echo 'Ambiguous instance selection. Refusing automatic termination.' >&2
      return 1
    fi
    run_aws ec2 terminate-instances --instance-ids "$instance_id" --output json
  fi
}

deployment_failed() {
  local code=$?
  trap - EXIT
  if (( code != 0 )); then
    terminate_own_worker || true
    # Cancel a continuing stack operation so it cannot launch a later instance.
    # ResultsBucket is retained by the template and expires its staged objects.
    run_aws cloudformation delete-stack --stack-name "$STACK_NAME" || true
    wait_stack_deleted || true
    echo 'Deployment failed. The private staging bucket may remain for inspection and explicit cleanup.' >&2
  fi
  exit "$code"
}

fetch_results() {
  local output_dir="$1" description bucket
  mkdir -p -- "$output_dir"
  description=$(stack_description)
  bucket=$(stack_output "$description" ResultsBucketName)
  run_aws s3api list-objects-v2 --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT" --prefix output/ --max-keys 1000 --output json > "$output_dir/objects.json"
  python3 - "$output_dir/objects.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
items=data.get('Contents',[])
allowed={'output/bootstrap.log'}|{'output/full/'+name for name in ('csv1.csv','csv2.csv','csv3.csv','run.log','progress.sqlite','cache.sqlite')}
if data.get('IsTruncated') or len(items)>len(allowed) or sum(x['Size'] for x in items)>256*1024*1024:
    raise SystemExit('Output listing exceeds approved export limits')
for obj in items:
    key=obj['Key']
    if key not in allowed:
        raise SystemExit('Unexpected result path')
PY
  while IFS= read -r key; do
    mkdir -p -- "$(dirname -- "$output_dir/${key#output/}")"
    run_aws s3api get-object --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT" --key "$key" "$output_dir/${key#output/}" --output json > /dev/null
  done < <(python3 -c 'import json,sys; print("\n".join(x["Key"] for x in json.load(open(sys.argv[1])).get("Contents",[])))' "$output_dir/objects.json")
  printf '%s\n' "$bucket" > "$output_dir/source_bucket.txt"
  printf '%s\n' "$STACK_NAME" > "$output_dir/source_stack.txt"
  echo "Downloaded the available full output to $output_dir. Validate it before cleanup."
}

verify_identity
case "$ACTION" in
  deploy)
    bundle="${3:?Provide the reviewed bundle zip}"
    if [[ ! -f "$bundle" ]] || (( $(stat -c %s "$bundle") > 33554432 )); then
      echo 'Bundle missing or larger than 32 MiB.' >&2
      exit 2
    fi
    active_workers=$(run_aws ec2 describe-instances --filters 'Name=tag:Project,Values=csv2-l2-sample-20261106,csv2-l2-full-20261106' 'Name=instance-state-name,Values=pending,running,stopping,stopped' --query 'Reservations[].Instances[].InstanceId' --output text)
    if [[ -n "$active_workers" && "$active_workers" != None ]]; then
      echo 'A sample or full worker still exists. Complete its shutdown and cleanup before launching this full run.' >&2
      exit 1
    fi
    bundle_sha=$(sha256sum "$bundle" | cut -d ' ' -f 1)
    expiry_utc=$(python3 - "$bundle" <<'PY'
import datetime,json,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    plan=json.loads(archive.read('full_plan.json'))
if plan.get('approval_recorded') is not True:raise SystemExit('Full scope has not been approved')
expiry=plan.get('expires_at_utc')
if not expiry:raise SystemExit('full_plan.json needs expires_at_utc')
instant=datetime.datetime.fromisoformat(expiry.replace('Z','+00:00'))
approved=datetime.datetime.fromisoformat(plan['approved_at_utc'].replace('Z','+00:00'))
if not 0<(instant-approved).total_seconds()<=21600:raise SystemExit('Expiry exceeds the approved six-hour window')
remaining=(instant-datetime.datetime.now(datetime.timezone.utc)).total_seconds()
if not 900<=remaining<=21600:raise SystemExit('Full approval must expire between 15 minutes and 6 hours from launch')
print(instant.strftime('%Y-%m-%dT%H:%M:%SZ'))
PY
)
    run_aws cloudformation validate-template --template-body "file://$TEMPLATE" --output json
    if stack_description > /dev/null 2>&1; then
      echo 'Stack already exists. Refusing to replace or resume a previous full.' >&2
      exit 1
    fi
    # Phase one creates the staging bucket before a worker can download the bundle.
    run_aws cloudformation create-stack --stack-name "$STACK_NAME" --template-body "file://$TEMPLATE" --capabilities CAPABILITY_IAM --parameters ParameterKey=WorkerEnabled,ParameterValue=false --tags Key=Project,Value=csv2-l2-full-20261106 --output json
    trap deployment_failed EXIT
    wait_stack CREATE_COMPLETE
    description=$(stack_description)
    bucket=$(stack_output "$description" ResultsBucketName)
    run_aws s3api put-object --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT" --key input/bundle.zip --body "$bundle" --server-side-encryption AES256 --output json
    run_aws cloudformation update-stack --stack-name "$STACK_NAME" --template-body "file://$TEMPLATE" --capabilities CAPABILITY_IAM --parameters ParameterKey=WorkerEnabled,ParameterValue=true "ParameterKey=BundleSha256,ParameterValue=$bundle_sha" "ParameterKey=ExpiryUtc,ParameterValue=$expiry_utc" --output json
    wait_stack UPDATE_COMPLETE
    trap - EXIT
    description=$(stack_description)
    echo "$description"
    echo 'The instance has a 330-minute shutdown timer and terminates on OS shutdown. Fetch output, validate it, then clean up this stack.'
    ;;
  fetch)
    fetch_results "${3:?Provide a result destination directory}"
    ;;
  cleanup)
    output_dir="${3:?Provide the validated fetched result directory}"
    if [[ ! -f "$output_dir/objects.json" || ! -f "$output_dir/source_bucket.txt" || ! -f "$output_dir/source_stack.txt" || ! -f "$output_dir/VALIDATED" ]]; then
      echo 'Cleanup requires fetched results and a VALIDATED marker written after verification.' >&2
      exit 2
    fi
    if [[ "$(cat "$output_dir/source_stack.txt")" != "$STACK_NAME" ]]; then
      echo 'Result stack does not match the cleanup target.' >&2
      exit 2
    fi
    description=$(stack_description)
    bucket=$(stack_output "$description" ResultsBucketName)
    if [[ "$(cat "$output_dir/source_bucket.txt")" != "$bucket" ]]; then
      echo 'Result bucket does not match the cleanup target.' >&2
      exit 2
    fi
    terminate_own_worker
    run_aws cloudformation delete-stack --stack-name "$STACK_NAME"
    wait_stack_deleted
    # The bucket is retained during stack deletion so results cannot disappear early.
    # Enumerate this exact private staging bucket and delete only its input/output keys.
    run_aws s3api list-objects-v2 --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT" --max-keys 1000 --output json > "$output_dir/cleanup_objects.json"
    python3 - "$output_dir/cleanup_objects.json" "$output_dir/cleanup_delete.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
if data.get('IsTruncated'):raise SystemExit('Unexpected truncated staging bucket listing')
keys=[x['Key'] for x in data.get('Contents',[])]
if any(not k.startswith(('input/','output/')) for k in keys):raise SystemExit('Unexpected staging object prefix')
json.dump({'Objects':[{'Key':k} for k in keys],'Quiet':True},open(sys.argv[2],'w'))
PY
    if python3 -c 'import json,sys; sys.exit(not bool(json.load(open(sys.argv[1]))["Objects"]))' "$output_dir/cleanup_delete.json"; then
      run_aws s3api delete-objects --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT" --delete "file://$output_dir/cleanup_delete.json" --output json
    fi
    run_aws s3api delete-bucket --bucket "$bucket" --expected-bucket-owner "$EXPECTED_ACCOUNT"
    echo 'Removed this full stack, terminated its worker, and removed its private staging bucket.'
    ;;
esac
