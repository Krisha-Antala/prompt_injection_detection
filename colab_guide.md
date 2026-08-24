# ☁️ Google Colab Setup Guide

This guide explains how to run the **Prompt Injection & Hallucination Guardrail System** in Google Colab, leveraging Google's free cloud servers and T4 GPU accelerators. This removes any CPU lag and download load from your local computer.

---

## 🛠️ Step-by-Step Setup

### Step 1: Open Google Colab
1. Go to **[colab.research.google.com](https://colab.research.google.com/)**.
2. Sign in with your Google account.
3. Click **New Notebook** (bottom right).

---

### Step 2: Enable GPU Acceleration (Highly Recommended)
Using a GPU speeds up the downloading and execution of the DistilBERT, DeBERTa NLI, and SBERT embedding models.
1. In the top menu, click **Runtime** > **Change runtime type**.
2. Under *Hardware accelerator*, select **T4 GPU** (or any available GPU).
3. Click **Save**.

---

### Step 3: Add your OpenAI API Key securely
1. Click the **key icon** (Secrets) in the left-hand sidebar of Google Colab.
2. Click **Add new secret**.
3. Set the **Name** to: `OPENAI_API_KEY`.
4. Set the **Value** to: *your actual OpenAI secret key (starting with `sk-proj-...`)*.
5. Toggle **Notebook access** to **ON** (enabled) for the secret.

---

### Step 4: Upload the Runner Script
1. Click the **folder icon** (Files) in the left-hand sidebar.
2. Drag and drop the **[colab_app.py](file:///c:/Users/KRISHA/Downloads/project/colab_app.py)** file from your computer's local directory (`c:\Users\KRISHA\Downloads\project\colab_app.py`) into the files area of Google Colab.

---

### Step 5: Execute the Server
Create a new code cell in your Colab notebook, paste the following Python code, and click the **Run (Play)** button:

```python
# 1. Fetch OpenAI API Key from Colab Secrets
import os
from google.colab import userdata

try:
    os.environ["OPENAI_API_KEY"] = userdata.get("OPENAI_API_KEY")
    print("✔ OpenAI API Key loaded successfully.")
except Exception:
    print("⚠ OpenAI API Key secret not found in Colab Secrets. Running in offline/simulation mode.")

# 2. Run the application setup and server
%run colab_app.py
```

---

## 🌐 Opening the Dashboard in Your Browser

As the code executes, it will print progress logs inside Colab (installing packages, writing templates, and downloading model weights). Once the Flask server is running, the script will output a clickable URL in the console:

> **`💡 [METHOD 1] Native Google Colab URL:`**  
> **`👉 CLICK HERE TO OPEN: https://localhost-5000-colab.googleusercontent.com/`**

Simply **click that link** to open the complete, gorgeous ChatGPT-styled Guardrail Dashboard in your browser! All processing (model checks, chatbot API queries, and training processes) will execute in Colab's cloud memory.
