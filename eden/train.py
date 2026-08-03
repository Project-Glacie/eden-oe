#!/usr/bin/env python3
"""Eden OE — Model Training CLI
Usage: eden train <model> --data <path> [--epochs 3] [--gpu 0]

Models: mind-4b, router-2b
Trains QLoRA on idle GPU. Outputs merged model to models/ directory.
Eve can trigger this during night cognition or on user request.
"""
import argparse, subprocess, os, sys
from pathlib import Path

EDEN_ROOT = Path(os.environ.get("EDEN_DATA", Path.home() / ".eden"))
MODELS_DIR = EDEN_ROOT / "models"
TRAINING_DIR = EDEN_ROOT / "training"

MODEL_CONFIGS = {
    "mind-4b": {
        "base": "Qwen/Qwen3-4B",
        "output": "eden-mind-4b",
        "r": 32, "alpha": 32, "epochs": 3,
        "gpu_vram": "3.3GB"
    },
    "router-2b": {
        "base": "Qwen/Qwen3-2B", 
        "output": "eden-router-2b",
        "r": 16, "alpha": 16, "epochs": 5,
        "gpu_vram": "1.8GB"
    }
}

def train(model_name, data_path, epochs=None, gpu=0):
    cfg = MODEL_CONFIGS.get(model_name)
    if not cfg:
        print(f"Unknown model: {model_name}. Available: {list(MODEL_CONFIGS.keys())}")
        return 1
    
    if not os.path.exists(data_path):
        print(f"Training data not found: {data_path}")
        return 1
    
    epochs = epochs or cfg["epochs"]
    output = MODELS_DIR / cfg["output"]
    
    print(f"Eden OE — Training {model_name}")
    print(f"  Base: {cfg['base']}")
    print(f"  Data: {data_path}")
    print(f"  Epochs: {epochs}")
    print(f"  GPU: {gpu} ({cfg['gpu_vram']} VRAM)")
    print(f"  Output: {output}")
    print()
    
    # Use the proven Python training pipeline
    script = Path(__file__).parent / "scripts" / "_train_model.py"
    if not script.exists():
        # Inline the training logic
        return _train_inline(cfg, data_path, epochs, gpu, output)
    
    env = os.environ.copy()
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    
    return subprocess.run([
        sys.executable, str(script),
        "--base", cfg["base"],
        "--data", data_path,
        "--epochs", str(epochs),
        "--r", str(cfg["r"]),
        "--alpha", str(cfg["alpha"]),
        "--output", str(output),
    ], env=env).returncode

def _train_inline(cfg, data_path, epochs, gpu, output):
    """Fallback inline trainer using the proven pipeline."""
    import torch, json
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from trl import SFTTrainer, SFTConfig
    
    print("Loading base model...")
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(cfg["base"], quantization_config=bnb, device_map="auto")
    tokenizer = AutoTokenizer.from_pretrained(cfg["base"]); tokenizer.pad_token = tokenizer.eos_token
    
    lora = LoraConfig(r=cfg["r"], lora_alpha=cfg["alpha"], lora_dropout=0.05,
        target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
        task_type="CAUSAL_LM")
    model = get_peft_model(model, lora)
    print(f"Trainable: {sum(p.numel() for p in model.parameters() if p.requires_grad):,}")
    
    with open(data_path) as f:
        data = [json.loads(l) for l in f if l.strip()]
    texts = [{"text": tokenizer.apply_chat_template(d["messages"], tokenize=False)} for d in data]
    dataset = Dataset.from_list(texts)
    print(f"Data: {len(dataset)} examples")
    
    trainer = SFTTrainer(model=model, processing_class=tokenizer, train_dataset=dataset,
        args=SFTConfig(per_device_train_batch_size=2, gradient_accumulation_steps=4,
            num_train_epochs=epochs, learning_rate=2e-4, bf16=True,
            logging_steps=10, output_dir="/tmp/eden_train",
            save_strategy="no", report_to="none",
            dataset_text_field="text", max_length=2048))
    trainer.train()
    
    output_path = Path(str(output))
    model.save_pretrained(str(output_path)); tokenizer.save_pretrained(str(output_path))
    model = model.merge_and_unload()
    model.save_pretrained(str(output_path) + "-merged"); tokenizer.save_pretrained(str(output_path) + "-merged")
    print(f"Done. Model at {output_path}-merged")
    return 0

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Eden OE Model Training")
    p.add_argument("model", choices=list(MODEL_CONFIGS.keys()))
    p.add_argument("--data", required=True)
    p.add_argument("--epochs", type=int)
    p.add_argument("--gpu", type=int, default=0)
    args = p.parse_args()
    sys.exit(train(args.model, args.data, args.epochs, args.gpu))
