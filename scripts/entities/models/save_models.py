#!/usr/bin/env python3
"""Train and persist MiniLM+XGBoost and SetFit entity classification models.

Usage:
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/save_models.py

Outputs:
    data/benchmark/minilm_xgboost/model.json   — XGBoost trained on MiniLM embeddings
    data/benchmark/setfit/model/                — SetFit model directory
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("save_models")

HERE = Path(__file__).resolve().parent.parent.parent
BENCHMARK = HERE / "data" / "benchmark"


def train_minilm_xgboost():
    """Train XGBoost on pre-computed MiniLM embeddings and save to disk."""
    log.info("=" * 50)
    log.info("MiniLM + XGBoost")

    # Load embeddings and labels
    train_emb = np.load(str(BENCHMARK / "embeddings_train_names.npy"))
    test_emb = np.load(str(BENCHMARK / "embeddings_test_names.npy"))

    with open(BENCHMARK / "train.json") as f:
        train_data = json.load(f)
    with open(BENCHMARK / "test.json") as f:
        test_data = json.load(f)

    y_train = np.array([d["label"] for d in train_data])
    y_test = np.array([d["label"] for d in test_data])

    log.info("  Train: %d samples (entity=%d, noise=%d)",
             len(y_train), int(y_train.sum()), int((1 - y_train).sum()))
    log.info("  Test:  %d samples (entity=%d, noise=%d)",
             len(y_test), int(y_test.sum()), int((1 - y_test).sum()))

    # Train XGBoost
    import xgboost as xgb

    model = xgb.XGBClassifier(
        n_estimators=500,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        use_label_encoder=False,
        random_state=42,
    )

    log.info("  Training XGBoost...")
    t0 = time.time()
    model.fit(
        train_emb, y_train,
        eval_set=[(test_emb, y_test)],
        verbose=False,
    )
    elapsed = time.time() - t0
    log.info("  Training complete in %.1f s", elapsed)

    # Evaluate
    train_acc = (model.predict(train_emb) == y_train).mean()
    test_acc = (model.predict(test_emb) == y_test).mean()
    log.info("  Train accuracy: %.4f", train_acc)
    log.info("  Test accuracy:  %.4f", test_acc)

    # Save
    out_dir = BENCHMARK / "minilm_xgboost"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.json"
    model.save_model(str(model_path))
    log.info("  Saved to %s", model_path)

    return model


def train_setfit():
    """Fine-tune a SetFit model and save to disk."""
    log.info("=" * 50)
    log.info("SetFit")

    from sentence_transformers import SentenceTransformer
    from setfit import SetFitModel, SetFitTrainer, TrainingArguments

    with open(BENCHMARK / "train.json") as f:
        train_data = json.load(f)
    with open(BENCHMARK / "val.json") as f:
        val_data = json.load(f)

    train_texts = [d["name"] for d in train_data]
    train_labels = [d["label"] for d in train_data]
    val_texts = [d["name"] for d in val_data]
    val_labels = [d["label"] for d in val_data]

    log.info("  Train: %d samples", len(train_texts))
    log.info("  Val:   %d samples", len(val_texts))

    from datasets import Dataset
    from sentence_transformers import SentenceTransformer, losses
    from sentence_transformers.trainer import SentenceTransformerTrainer
    from sentence_transformers.training_args import SentenceTransformerTrainingArguments

    log.info("  Training SetFit (contrastive fine-tune + logistic regression)...")

    log.info("  Loading base SentenceTransformer (all-MiniLM-L6-v2)...")
    t0 = time.time()
    encoder = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("  Base model loaded in %.1f s", time.time() - t0)

    # We need to create sentence-pair data for contrastive loss
    # For entity vs noise: same-label pairs are similar (label=1), different-label pairs are dissimilar (label=0)
    import numpy as np
    rng = np.random.RandomState(42)

    # Create pairs
    pairs_s1, pairs_s2, pair_labels = [], [], []
    labels_arr = np.array(train_labels)
    entity_indices = np.where(labels_arr == 1)[0]
    noise_indices = np.where(labels_arr == 0)[0]

    # Positive pairs (entity-entity, noise-noise)
    for _ in range(min(500, len(entity_indices) // 2)):
        i1, i2 = rng.choice(entity_indices, 2, replace=False)
        pairs_s1.append(train_texts[i1])
        pairs_s2.append(train_texts[i2])
        pair_labels.append(1)
    for _ in range(min(500, len(noise_indices) // 2)):
        i1, i2 = rng.choice(noise_indices, 2, replace=False)
        pairs_s1.append(train_texts[i1])
        pairs_s2.append(train_texts[i2])
        pair_labels.append(1)

    # Negative pairs (entity-noise)
    n_neg = 1000
    for _ in range(n_neg):
        i1 = rng.choice(entity_indices)
        i2 = rng.choice(noise_indices)
        pairs_s1.append(train_texts[i1])
        pairs_s2.append(train_texts[i2])
        pair_labels.append(0)

    pairs_ds = Dataset.from_dict({
        "sentence_1": pairs_s1,
        "sentence_2": pairs_s2,
        "label": pair_labels,
    })
    log.info("  Created %d contrastive training pairs", len(pairs_ds))

    # Fine-tune encoder with contrastive loss
    loss = losses.ContrastiveLoss(encoder)
    args = SentenceTransformerTrainingArguments(
        output_dir=str(BENCHMARK / "setfit" / "tmp"),
        num_train_epochs=2,
        per_device_train_batch_size=64,
        learning_rate=2e-5,
        warmup_ratio=0.1,
        logging_steps=20,
        save_strategy="no",
        report_to="none",
        disable_tqdm=True,
    )

    trainer = SentenceTransformerTrainer(
        model=encoder,
        args=args,
        train_dataset=pairs_ds,
        loss=loss,
    )

    log.info("  Fine-tuning encoder...")
    t0 = time.time()
    trainer.train()
    elapsed = time.time() - t0
    log.info("  Fine-tuning complete in %.1f s", elapsed)

    # Train logistic regression classifier on sentence embeddings
    from sklearn.linear_model import LogisticRegression
    log.info("  Training logistic regression classifier...")
    train_emb = encoder.encode(train_texts, show_progress_bar=False)
    val_emb = encoder.encode(val_texts, show_progress_bar=False)
    clf = LogisticRegression(max_iter=1000, random_state=42)
    clf.fit(train_emb, train_labels)
    val_preds = clf.predict(val_emb)
    val_acc = (val_preds == val_labels).mean()
    log.info("  Val accuracy: %.4f", val_acc)

    # Save
    out_dir = BENCHMARK / "setfit" / "model"
    out_dir.mkdir(parents=True, exist_ok=True)
    encoder.save(str(out_dir / "encoder"))
    import joblib
    joblib.dump(clf, str(out_dir / "classifier.joblib"))
    log.info("  Saved encoder to %s", out_dir / "encoder")
    log.info("  Saved classifier to %s", out_dir / "classifier.joblib")

    # Quick test
    test_preds = clf.predict(val_emb[:5])
    log.info("  Sanity check (first 5 val preds): %s", test_preds.tolist())

    return {"encoder": encoder, "classifier": clf}


def main():
    train_minilm_xgboost()
    train_setfit()
    log.info("=" * 50)
    log.info("All models saved successfully.")


if __name__ == "__main__":
    main()
