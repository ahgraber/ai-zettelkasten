# NVIDIA GPU Setup for Podman on Debian 13 (Trixie)

This guide describes how to install NVIDIA drivers and enable GPU passthrough in Podman containers on Debian 13 using the NVIDIA Container Toolkit with CDI (Container Device Interface).

______________________________________________________________________

## ✅ Prerequisites

- An NVIDIA GPU (e.g. RTX 3090)
- Debian 13 (Trixie)
- Podman installed (`sudo apt install podman`)
- NVIDIA drivers installed and working on the host (`nvidia-smi` should return output)

______________________________________________________________________

## 🔧 Step 1: Install the NVIDIA Driver

### 1. Enable `non-free` and `contrib` repositories

```bash
sudo sed -i 's/main/main non-free contrib/g' /etc/apt/sources.list
sudo apt update
```

### 2. Install required build tools

```bash
sudo apt install linux-headers-$(uname -r) build-essential dkms nvidia-detect
```

### 3. Install the recommended NVIDIA driver

```bash
sudo apt install nvidia-driver nvidia-kernel-dkms
sudo reboot
```

After rebooting, confirm GPU access with:

```bash
nvidia-smi
```

You should see your GPU and driver version listed.

Reference: [https://linuxconfig.org/debian-13-nvidia-driver-installation](https://linuxconfig.org/debian-13-nvidia-driver-installation)

______________________________________________________________________

## 🔧 Step 2: Install NVIDIA Container Toolkit with CDI Support

### 1. Install NVIDIA GPG Key

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg
```

### 2. Add NVIDIA APT Repository (Release-compatible)

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb #deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] #' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
```

### 3. Install the Toolkit

```bash
sudo apt update
sudo apt install -y nvidia-container-toolkit nvidia-container-toolkit-base
```

### 4. Generate CDI Device Specification

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

Reference: [https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

______________________________________________________________________

## ✅ Step 3: Test GPU Access with Podman

Test with the latest CUDA image from Docker Hub:

```bash
sudo podman run --rm --device nvidia.com/gpu=all \
  docker.io/nvidia/cuda:12.0.1-base-ubuntu22.04 nvidia-smi
```

You should see the same output as the host `nvidia-smi`, confirming GPU passthrough.
