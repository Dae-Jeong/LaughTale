#!/usr/bin/env bash
# Bounded opt-in experiment. Restores the exact pre-experiment lab settings.
set -u
KUBE=(kubectl --context k3d-laughtale-local -n laughtale-chat-external)
if ! "${KUBE[@]}" get deployment chat -o json | jq -e '
  .spec.replicas == 2 and
  (.spec.template.spec.containers[] | select(.name == "chat") |
    .image == "laughtale-chat:auth-metrics-v1" and .resources.limits.cpu == "1" and
    ([.env[]? | select(.name == "LAB_MESSAGE_ADMISSION_LIMIT")] | length) == 0)
' >/dev/null; then
  echo 'Baseline differs from the documented restoration target; no changes made.'
  exit 1
fi
restore() {
  "${KUBE[@]}" patch deployment chat --type strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"chat","image":"laughtale-chat:auth-metrics-v1","resources":{"limits":{"cpu":"1"}},"env":[{"name":"LAB_MESSAGE_ADMISSION_LIMIT","$patch":"delete"}]}]}}}}'
  "${KUBE[@]}" rollout status deployment/chat --timeout=60s
}
trap restore EXIT
for limit in 0 4; do
  old_pods=()
  while IFS= read -r pod; do
    old_pods+=("$pod")
  done < <("${KUBE[@]}" get pods -l app=chat -o name)
  if [ "${#old_pods[@]}" -ne 2 ]; then
    echo 'Expected exactly two stable API pods; no experiment started.'
    exit 1
  fi
  "${KUBE[@]}" patch deployment chat --type strategic -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"chat\",\"image\":\"laughtale-chat:admission-probe-v1\",\"resources\":{\"limits\":{\"cpu\":\"300m\"}},\"env\":[{\"name\":\"LAB_MESSAGE_ADMISSION_LIMIT\",\"value\":\"$limit\"}]}]}}}}" || exit 1
  "${KUBE[@]}" rollout status deployment/chat --timeout=60s || exit 1
  "${KUBE[@]}" wait --for=delete "${old_pods[@]}" --timeout=60s || exit 1
  echo "Admission limit: $limit"
  # A failed throughput criterion is a measured outcome, not a reason to skip B.
  services/chat/.venv/bin/python tests/load/chat-internal/lock_probe.py --rate 50 --seconds 60 --rooms 1
  probe_exit=$?
  if [ "$probe_exit" -gt 1 ]; then
    echo 'Incomplete probe; stopping the sequence and restoring the lab.'
    exit 1
  fi
done
