from flask import Flask, Response, request
from prometheus_client import Gauge, Counter, Histogram, generate_latest
import requests
import time

app = Flask(__name__)

# Ollama model metrics
num_models = Gauge('ollama_models_loaded', 'Number of models loaded in Ollama')

# Per-inference metrics
inference_count = Counter('ollama_inference_requests_total', 'Total number of inference requests')
inference_latency = Histogram('ollama_inference_duration_seconds', 'Time taken for each inference request')
input_tokens = Counter('ollama_input_tokens_total', 'Total number of input tokens processed')
output_tokens = Counter('ollama_output_tokens_total', 'Total number of output tokens generated')

@app.route('/metrics')
def metrics():
    try:
        res = requests.get("http://ollama:11434/api/ps").json()
        num_models.set(len(res.get("models", [])))
    except Exception:
        num_models.set(0)
    return Response(generate_latest(), mimetype='text/plain')


@app.route('/track_inference', methods=['POST'])
@inference_latency.time()
def track_inference():
    data = request.json
    inference_count.inc()

    # Extract token info
    prompt_tokens = data.get("prompt_tokens", 0)
    generated_tokens = data.get("generated_tokens", 0)

    input_tokens.inc(prompt_tokens)
    output_tokens.inc(generated_tokens)

    return {"status": "ok"}, 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)

