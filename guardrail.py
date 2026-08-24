import os
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from detector import PromptInjectionDetector, HallucinationDetector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BLOCKED_PROMPT_RESPONSE = "We cannot guide you for this prompt. Please ask me another safe question."

GEMINI_MODEL_FALLBACKS = [
    os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
    "gemini-3.6-flash",
    "gemini-2.5-flash",
]

GROQ_MODEL_FALLBACKS = [
    os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "groq/compound-mini",
]

# System style: concise chat answers, but allow tables/headers when user asks for comparison
SYSTEM_STYLE = (
    "You are GuardrailAI, a helpful chat assistant. "
    "Answer concisely in short paragraphs or bullet points (under ~150 words). "
    "If the user explicitly asks for a comparison, difference, pros/cons, or structured breakdown, "
    "then use a clean markdown table with headers. "
    "Avoid unnecessary markdown headers (##), horizontal rules (---), or oversized tables for simple questions. "
    "Use **bold** for key terms. Keep responses visually scannable."
)


def _payload_without_thinking(payload):
    """Copy of the request payload with thinkingConfig stripped (for models that reject it)."""
    clone = {
        "contents": payload.get("contents", []),
        "generationConfig": {
            k: v for k, v in payload.get("generationConfig", {}).items()
            if k != "thinkingConfig"
        },
    }
    return clone


class GuardrailException(Exception):
    """Custom exception raised when security or grounding rules are violated."""
    def __init__(self, message, category, details=None):
        super().__init__(message)
        self.category = category
        self.details = details or {}


class GuardrailWrapper:
    def __init__(self, custom_model_path=None, injection_detector=None, hallucination_detector=None):
        """
        Multi-provider guardrail wrapper (Gemini / Groq / OpenAI) with automatic fallback.

        injection_detector / hallucination_detector can be shared instances so the
        heavy transformer models are loaded only once per process.
        """
        self.injection_detector = injection_detector or PromptInjectionDetector(custom_model_path)
        self._hallucination = hallucination_detector

        # Reusable HTTP session (keeps TCP connections alive -> faster repeat calls)
        self.session = requests.Session()
        # Auto-retry transient network/DNS blips instead of failing over instantly
        retry = Retry(total=3, connect=3, read=1, backoff_factor=0.5,
                      status_forcelist=[500, 502, 503, 504], allowed_methods=["POST"])
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Load API keys from environment
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")

        # Clean placeholder values
        placeholders = {"your_gemini_api_key_here", "your_groq_api_key_here", "your_openai_api_key_here", "your_new_key_here", "", None}
        if self.gemini_key and self.gemini_key.strip().lower() in placeholders:
            self.gemini_key = None
        if self.groq_key and self.groq_key.strip().lower() in placeholders:
            self.groq_key = None
        if self.openai_key and (self.openai_key.strip().lower() in placeholders or not self.openai_key.startswith("sk-")):
            self.openai_key = None

        # Remember which Gemini model worked to avoid repeated 404 retries
        self._gemini_model = None
        # Remember which Groq model worked (Groq retires models too)
        self._groq_model = None
        # Remember which models accept thinkingConfig (some 400 on it)
        self._thinking_ok = {}

        # Providers that hit quota (429) are skipped for 10 minutes so
        # every prompt doesn't waste time calling dead providers.
        self._provider_cooldown = {}

        print("[+] Guardrail initialized with available providers:")
        print(f"    - Google Gemini : {'Active' if self.gemini_key else 'Not Configured'}")
        print(f"    - Groq (Llama 3): {'Active' if self.groq_key else 'Not Configured'}")
        print(f"    - OpenAI ChatGPT: {'Active' if self.openai_key else 'Not Configured'}")

    @property
    def hallucination_detector(self):
        """Lazily create the hallucination detector only when first needed."""
        if self._hallucination is None:
            self._hallucination = HallucinationDetector()
        return self._hallucination

    def set_injection_detector(self, detector):
        """Swap in a newly trained detector without rebuilding the whole wrapper."""
        self.injection_detector = detector

    def set_hallucination_detector(self, detector):
        self._hallucination = detector

    def query_gemini(self, user_prompt, context=None):
        """Calls Google Gemini API with automatic model fallback for retired models."""
        if not self.gemini_key:
            return None, "No Gemini API key configured"

        prompt_text = SYSTEM_STYLE + "\n\n" + user_prompt
        if context:
            prompt_text = SYSTEM_STYLE + "\n\n" + f"Context: {context}\n\nQuestion: {user_prompt}\n\nAnswer using the provided context accurately."

        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "temperature": 0.2,
                # Thinking models burn tokens on hidden reasoning -> keep a generous budget
                # and disable "thinking" for fast, deterministic Q&A.
                "maxOutputTokens": 2048,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

        # Try the remembered working model first, then the full fallback list
        candidates = [self._gemini_model] if self._gemini_model else []
        for m in GEMINI_MODEL_FALLBACKS:
            if m and m not in candidates:
                candidates.append(m)

        url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        last_err = "Unknown Gemini error"
        for model in candidates:
            attempts = [payload, _payload_without_thinking(payload)]
            if self._thinking_ok.get(model) is False:
                attempts = attempts[1:]  # known to reject thinkingConfig -> skip the wasted call
            for attempt_payload in attempts:
                try:
                    response = self.session.post(
                        url.format(model=model),
                        params={"key": self.gemini_key},
                        json=attempt_payload,
                        timeout=30,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        candidate = data.get("candidates", [{}])[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        # Only keep final-answer text; skip any leaked "thought" parts
                        text = "".join(
                            p.get("text", "") for p in parts if not p.get("thought")
                        ).strip()
                        if text:
                            self._gemini_model = model
                            if attempt_payload is payload:
                                self._thinking_ok[model] = True
                            return text, None
                        last_err = f"Empty response from Gemini (finishReason={candidate.get('finishReason', 'unknown')})"
                        break
                    else:
                        body = response.text[:150]
                        if response.status_code == 429:
                            # Quota is project-wide -> no point trying other models
                            return None, f"Gemini Error (429): {body}"
                        # Some models reject thinkingConfig with a generic 400 -> retry without it
                        if response.status_code == 400 and attempt_payload is payload:
                            self._thinking_ok[model] = False
                            last_err = f"Gemini 400 on {model}; retrying without thinkingConfig"
                            continue
                        last_err = f"Gemini Error ({response.status_code}): {body}"
                        break
                except Exception as e:
                    last_err = f"Gemini Exception: {str(e)}"
                    break

        return None, last_err

    def query_groq(self, user_prompt, context=None):
        """Calls Groq Cloud API with automatic model fallback (free ultra-fast tier)."""
        if not self.groq_key:
            return None, "No Groq API key configured"

        messages = [{"role": "system", "content": SYSTEM_STYLE}]
        if context:
            messages.append({"role": "system", "content": f"You are a helpful, factual assistant. Use this context to answer: {context}"})
        messages.append({"role": "user", "content": user_prompt})

        payload_base = {
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 800,
        }

        # Try the remembered working model first, then the fallback list
        candidates = [self._groq_model] if self._groq_model else []
        for m in GROQ_MODEL_FALLBACKS:
            if m and m not in candidates:
                candidates.append(m)

        last_err = "Unknown Groq error"
        for model in candidates:
            payload = dict(payload_base, model=model)
            try:
                response = self.session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=20,
                )
                if response.status_code == 200:
                    choices = response.json().get("choices", [])
                    if choices and "message" in choices[0]:
                        text = choices[0]["message"]["content"].strip()
                        if text:
                            self._groq_model = model  # remember for future calls
                            return text, None
                    last_err = "Empty response from Groq"
                else:
                    body = response.text[:150]
                    if response.status_code == 429:
                        # Quota/rate limit is account-wide -> stop here, cooldown handles the rest
                        return None, f"Groq Error (429): {body}"
                    last_err = f"Groq Error ({response.status_code}): {body}"
                    if response.status_code == 404:
                        continue  # retired/unavailable model -> try next in list
                    break
            except Exception as e:
                last_err = f"Groq Exception: {str(e)}"
                break

        return None, last_err

    def query_openai(self, user_prompt, context=None):
        """Calls OpenAI ChatGPT API (gpt-4o-mini)."""
        if not self.openai_key:
            return None, "No OpenAI API key configured"

        messages = [{"role": "system", "content": SYSTEM_STYLE}]
        if context:
            messages.append({"role": "system", "content": f"You are a helpful assistant. Use this context to answer: {context}"})
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 800,
        }

        try:
            response = self.session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.openai_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            if response.status_code == 200:
                choices = response.json().get("choices", [])
                if choices and "message" in choices[0]:
                    return choices[0]["message"]["content"].strip(), None
                return None, "Empty response from OpenAI"
            return None, f"OpenAI Error ({response.status_code}): {response.text[:120]}"
        except Exception as e:
            return None, f"OpenAI Exception: {str(e)}"

    def generate_llm_response(self, user_prompt, context=None, preferred_provider="auto"):
        """
        Queries the preferred LLM provider with automatic multi-provider fallback.
        Priority: Preferred -> Gemini -> Groq -> OpenAI -> Simulation Fallback.
        Unconfigured providers are skipped entirely.
        """
        providers = {
            "gemini": (self.query_gemini, "Google Gemini"),
            "groq": (self.query_groq, "Groq (GPT-OSS 120B)"),
            "openai": (self.query_openai, "OpenAI (gpt-4o-mini)"),
        }

        configured = [
            p for p in ["gemini", "groq", "openai"]
            if getattr(self, f"{p}_key") and self._provider_cooldown.get(p, 0) < time.time()
        ]
        call_order = []
        if preferred_provider in providers and preferred_provider in configured:
            call_order.append(preferred_provider)
        for p in configured:
            if p not in call_order:
                call_order.append(p)

        for p in call_order:
            func, name = providers[p]
            ans, err = func(user_prompt, context)
            if ans:
                return ans, name, True
            if err:
                if "(429)" in err or "quota" in err.lower() or "Free usage" in err:
                    # Quota exhausted: park this provider for 10 minutes
                    self._provider_cooldown[p] = time.time() + 600
                    print(f"[!] {name} quota exhausted. Cooling down 10 min, falling back...")
                elif "No " not in err:
                    print(f"[!] {name} failed: {err}. Falling back to next available provider...")

        # Final Fallback to offline simulation if no live API answered
        return self._simulate_llm_response(user_prompt, context), "Offline Simulation", False

    def run_safe_query(self, user_prompt, context=None, provider="auto"):
        """
        Full dual-layer guardrail pipeline:
        1. Pre-execution Prompt Injection & Cyber Threat Scan
        2. Live Multi-LLM Query (Gemini / Groq / OpenAI) with automatic fallback
        3. Post-execution Factual Consistency & Hallucination Scan
        """
        result = {
            "safe": True,
            "status": "Success",
            "prompt_injection_score": 0.0,
            "injection_method": "none",
            "hallucination_score": 0.0,
            "output_text": "",
            "llm_mode": "live",
            "provider_used": "auto",
            "processed_by_model": False,
            "counted": True,
            "context_used": context,
        }

        # --- 1. Scan Input for Prompt Injection & Cyber Threats ---
        injection_result = self.injection_detector.predict(user_prompt)
        result["prompt_injection_score"] = injection_result["score"]
        result["injection_method"] = injection_result["method"]

        if injection_result["injection_detected"] and injection_result["method"] == "heuristic":
            result["safe"] = False
            result["status"] = "Blocked: Malicious Prompt Detected"
            result["output_text"] = BLOCKED_PROMPT_RESPONSE
            result["llm_mode"] = "blocked"
            result["provider_used"] = "Security Guardrail"
            result["processed_by_model"] = False
            result["counted"] = False
            return result

        if injection_result["injection_detected"]:
            result["transformer_warning"] = "Transformer flagged this prompt as anomalous."

        # --- 2. Query Live LLM Provider ---
        llm_response, provider_name, is_live = self.generate_llm_response(user_prompt, context, provider)
        result["output_text"] = llm_response
        result["provider_used"] = provider_name
        result["llm_mode"] = "live" if is_live else "simulation"
        result["processed_by_model"] = True

        # --- 3. Scan Output for Hallucination & Factual Consistency ---
        if context:
            hallucination_result = self.hallucination_detector.predict(context, llm_response)
            result["hallucination_score"] = round(1.0 - hallucination_result["grounding_score"], 4)

            if hallucination_result["hallucination_detected"]:
                result["safe"] = False
                result["status"] = "Blocked: Response Hallucination Detected"
                result["output_text"] = "Warning: The generated response contains unverified information unsupported by the context database."

        return result

    def _simulate_llm_response(self, prompt, context):
        """Simulates intelligent offline answers when no live API keys are connected."""
        prompt_lower = prompt.lower()

        if "water" in prompt_lower and context:
            if "boiling point" in prompt_lower or "boil" in prompt_lower:
                return "Based on the provided context, water boils at 100 degrees Celsius or 212 degrees Fahrenheit at sea level."
            else:
                return "Water is a compound of nitrogen and chlorine that freezes at 50 degrees Fahrenheit."

        if "eiffel" in prompt_lower and context:
            return "The Eiffel Tower is situated on the Champ de Mars in Paris, France, and was built in 1889 by engineer Gustave Eiffel."

        if ("photosynthesis" in prompt_lower or "glucose" in prompt_lower or "plants" in prompt_lower) and context:
            return "Based on the context, plants make glucose and oxygen using carbon dioxide, water, and sunlight."

        return self._generate_offline_answer(prompt, context)

    def _generate_offline_answer(self, prompt, context=None):
        clean_prompt = " ".join(prompt.strip().split())
        prompt_lower = clean_prompt.lower()

        if not clean_prompt:
            return "Please enter a question or message, and I will help."

        if context:
            return f"Based on the reference context, regarding '{clean_prompt}': The details confirm the facts provided in the knowledge context."

        if re.search(r"\b(hi|hello|hey|good morning|good evening)\b", prompt_lower):
            return "Hello! I am GuardrailAI assistant. How can I help you today?"

        return (
            f"You asked: '{clean_prompt}'. Prompt passed all security guardrail checks. "
            "To enable live, detailed AI answers across all topics, connect a free Google Gemini or Groq API key in your .env file."
        )
