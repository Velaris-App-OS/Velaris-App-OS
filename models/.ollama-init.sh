#!/bin/bash
# Ollama auto-import: scans /models for *.gguf files and registers each one.
# Runs as the Ollama container entrypoint — starts the Ollama server, then imports.
# Client workflow: drop a .gguf in the repo-root models/ folder and restart the container.

# Start Ollama server in the background
ollama serve &
OLLAMA_PID=$!

# Wait for the server to be ready
echo "[ollama-init] Waiting for Ollama server..."
until curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; do
  sleep 1
done
echo "[ollama-init] Server ready."

# Import every .gguf found in /models (skipping this script itself)
for gguf in /models/*.gguf; do
  [ -f "$gguf" ] || continue
  model_name=$(basename "$gguf" .gguf)
  # Check if already imported to avoid re-importing on every restart
  if ollama list | grep -q "^${model_name}:"; then
    echo "[ollama-init] Already registered: ${model_name} — skipping"
    continue
  fi
  echo "[ollama-init] Importing ${model_name} from ${gguf}..."
  printf 'FROM %s\n' "$gguf" | ollama create "$model_name" -f -
  echo "[ollama-init] Done: ${model_name}"
done

echo "[ollama-init] All models ready. Handing off to Ollama server."
wait "$OLLAMA_PID"
