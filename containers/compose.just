# Start or stop a throwaway RustFS S3 store on localhost:9000 with the `aizk` bucket.
dev ACTION:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ ACTION }}" in
        up) podman-compose -f podman-compose.dev.yaml up -d ;;
        down) podman-compose -f podman-compose.dev.yaml down ;;
        *) echo "usage: just compose dev up|down" >&2; exit 2 ;;
    esac
