# Docling Inference

Create a reliable, GPU-enabled Docling deployment inside containers and pair it with local language models on an RTX 3090 workstation.

## Host and Runtime Prerequisites

1. **Install the NVIDIA driver.** Use your distribution's package manager or the official installer.
   Verify the host sees the GPU before continuing:

   ```sh
   nvidia-smi
   ```

2. **Install the NVIDIA Container Toolkit.** Follow NVIDIA's installation guide for Docker or Podman, then configure the runtime:

   ```sh
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```

   Replace `docker` with `containerd` or `podman` if you use an alternate runtime.

3. **Confirm GPU passthrough.** Run a CUDA base image with GPU flags to ensure containers can see the device:

   ```sh
   docker run --rm --gpus all nvidia/cuda:12.0.1-base-ubuntu22.04 nvidia-smi
   ```

   If the command prints the driver and GPU details, the passthrough is working.

### Troubleshooting checklist

- `docker info | grep -i runtime` should list `nvidia`.
- Podman users must enable the `nvidia` container runtime hook and, for rootless setups, set `no-cgroups=true` in `/etc/nvidia-container-runtime/config.toml`.
- Reboot after major driver or toolkit upgrades to avoid stale libraries.

## Running Docling with `docling-serve`

Docling publishes a REST API server image that requires no additional code.

1. **Fetch the image.**

   ```sh
   docker pull quay.io/docling-project/docling-serve:latest
   ```

2. **Start the service.**

   ```sh
   docker run \
     --detach \
     --name docling-serve \
     --gpus all \
     --publish 5001:5001 \
     --volume "$PWD/data:/data:rw" \
     quay.io/docling-project/docling-serve:latest
   ```

   - Port `5001` exposes both the OpenAPI UI at `http://localhost:5001/docs` and the web UI at `/ui`.
   - Mount a writable directory if you want to persist uploads or outputs.
   - Use environment variables (for example, `--env DOCSERVE_ALLOWED_ORIGINS=*`) to control service settings.

3. **Test the API.**

   ```sh
   curl -X POST \
     -F "file=@/path/to/document.pdf" \
     http://localhost:5001/v1alpha/convert/source
   ```

   Expect JSON output describing the parsed document.

### Observing GPU utilization

- Watch the container log for PyTorch to confirm CUDA is detected:

  ```sh
  docker logs docling-serve | grep -i cuda
  ```

- Use `nvidia-smi` while a conversion runs to confirm activity spikes.

- If the container falls back to CPU, rebuild with CUDA-enabled dependencies (see below).

## Building a Custom Docling Image

Use the official image as a base unless you require additional libraries. To add packages while keeping GPU support, start from an NVIDIA CUDA runtime image and install Docling yourself:

```Dockerfile
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip && pip install docling[serve]
EXPOSE 5001
CMD ["docling-serve", "--host", "0.0.0.0", "--port", "5001"]
```

Build and run with GPU access:

```sh
docker build -t docling-custom .
docker run --rm --gpus all -p 5001:5001 docling-custom
```

Keep the image lean by removing build tools after installation and by pinning versions in a `requirements.txt` file when reproducibility matters.

## Sidebar: Serving Local LLMs Alongside Docling

Run language models in parallel containers so you can prototype embeddings or completions locally.

### Option A: Ollama

Ollama provides an OpenAI-compatible HTTP API for many open models.

```sh
docker run \
  --detach \
  --name ollama \
  --gpus all \
  --publish 11434:11434 \
  ollama/ollama:latest
```

- Use `ollama run mistral` inside the container to pull and start a model.
- Expose `OLLAMA_ORIGINS` or `OLLAMA_HOST` environment variables if you need cross-origin or remote access.
- The API becomes reachable at `http://localhost:11434`.

### Option B: Custom FastAPI service

If you need bespoke endpoints, build a FastAPI application that loads your preferred Transformer or embedding model and expose it via Uvicorn:

```sh
docker run \
  --detach \
  --name local-llm \
  --gpus all \
  --publish 8000:8000 \
  --env MODEL_NAME=bge-large-en-v1.5 \
  your-fastapi-llm-image:latest
```

- Configure model paths, cache directories, and authentication via environment variables passed at runtime.
- Instrument the service with request timeouts and logging for easier debugging.

## Coordinating Services

- Use Docker Compose or Kubernetes to run Docling, Ollama, and auxiliary APIs together. Assign explicit container names and publish only the ports you need.
- Persist shared data (such as converted documents) in volumes mounted into each container.
- Add a reverse proxy (for example, Traefik or Nginx) to enforce HTTPS and request limits before exposing endpoints outside your workstation.

## References

- NVIDIA Container Toolkit Install Guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- Docling Installation Guide: <https://docling-project.github.io/docling/installation/>
- Docling GitHub Repository: <https://github.com/docling-project/docling>
- Ollama GPU Support Overview: <https://docs.ollama.com/gpu>
