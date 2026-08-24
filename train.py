import os
import json
import torch
from torch.utils.data import Dataset
from sklearn.model_selection import train_test_split
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments
from dataset_generator import save_datasets

# Define paths
DATA_DIR = "data"
MODEL_DIR = "models/injection_detector"
INJECTIONS_FILE = os.path.join(DATA_DIR, "injections.json")

# Ensure dataset exists
if not os.path.exists(INJECTIONS_FILE):
    print("Dataset files not found. Triggering dataset generation...")
    save_datasets(DATA_DIR)

class InjectionDataset(Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)

def train_model():
    print("Starting prompt injection detector training pipeline...")
    
    # 1. Load data
    with open(INJECTIONS_FILE, "r") as f:
        data = json.load(f)
        
    texts = [item["text"] for item in data]
    labels = [item["label"] for item in data]
    
    print(f"Loaded {len(texts)} samples (Positive/Injection: {sum(labels)}, Negative/Safe: {len(labels) - sum(labels)})")
    
    # Split train/eval
    train_texts, val_texts, train_labels, val_labels = train_test_split(
        texts, labels, test_size=0.2, random_state=42, stratify=labels
    )
    
    # 2. Initialize tokenizer and model
    model_name = "distilbert-base-uncased"
    print(f"Loading base tokenizer and model: {model_name}...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=2)
    
    # Tokenize
    print("Tokenizing datasets...")
    train_encodings = tokenizer(train_texts, truncation=True, padding=True, max_length=128)
    val_encodings = tokenizer(val_texts, truncation=True, padding=True, max_length=128)
    
    # Create datasets
    train_dataset = InjectionDataset(train_encodings, train_labels)
    val_dataset = InjectionDataset(val_encodings, val_labels)
    
    # 3. Setup training arguments
    print("Setting up training configurations...")
    # Adjust for CPU-friendly training
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    training_args = TrainingArguments(
        output_dir="./results",
        num_train_epochs=3,
        per_device_train_batch_size=8 if device == "cuda" else 4,
        per_device_eval_batch_size=8,
        warmup_steps=10,
        weight_decay=0.01,
        logging_dir="./logs",
        logging_steps=5,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        # Disable progress bar to prevent terminal spam
        disable_tqdm=True,
        use_cpu=(device == "cpu")
    )
    
    # 4. Initialize Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        tokenizer=tokenizer
    )
    
    # 5. Train
    print(f"Training on device: {device.upper()}. Please wait...")
    trainer.train()
    
    # 6. Save model
    print(f"Saving fine-tuned model and tokenizer to: {MODEL_DIR}")
    os.makedirs(MODEL_DIR, exist_ok=True)
    trainer.save_model(MODEL_DIR)
    tokenizer.save_pretrained(MODEL_DIR)
    
    print("Training successfully completed and model saved!")

if __name__ == "__main__":
    train_model()
