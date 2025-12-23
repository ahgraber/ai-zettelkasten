#!/bin/bash
# Script to download stronger models for use with Docling in Docker

# Set the models directory
MODELS_DIR="./models"

docling-tools models download --all -o "${MODELS_DIR}"

echo "Downloading stronger VLM models to ${MODELS_DIR} ..."

# Download Granite Vision 3.3 2B (stronger than SmolVLM)
echo "Downloading ibm-granite/granite-vision-3.3-2b..."
huggingface-cli download ibm-granite/granite-vision-3.3-2b \
  --local-dir "${MODELS_DIR}/ibm-granite--granite-vision-3.3-2b" \
  --local-dir-use-symlinks False

# Download Qwen2.5-VL-3B (even stronger)
echo "Downloading Qwen/Qwen2.5-VL-3B..."
huggingface-cli download Qwen/Qwen2.5-VL-3B \
  --local-dir "${MODELS_DIR}/Qwen--Qwen2.5-VL-3B" \
  --local-dir-use-symlinks False

# Download Microsoft Phi-4 (strong reasoning)
echo "Downloading microsoft/Phi-4..."
huggingface-cli download microsoft/Phi-4 \
  --local-dir "${MODELS_DIR}/microsoft--Phi-4" \
  --local-dir-use-symlinks False

echo "Models downloaded! Update your Docker container environment:"
echo "DOCLING_SERVE_ARTIFACTS_PATH=/opt/app-root/src/models"
