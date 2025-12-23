# Docling Server

Requires NVIDIA GPU.

## Quickstart

```sh
podman compose -f docling-compose.yaml up
```

Demo UI should be available at <http://10.2.1.102:5001/ui/>

## Preload / Cache models

Uncomment and run the `docling-model-cache-load` service

## GPU utilization stats

```sh
watch -n 1 nvidia-smi
```

or use `gpustat`

```sh
uv tool install gpustat # first run
# launch nvidia-smi daemon
sudo nvidia-smi daemon
#
gpustat --show-power draw --no-process --watch 1
```

or use `nvtop`

```sh
# sudo apt install nvtop
nvtop
```
