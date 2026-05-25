import re
import time
import logging

log = logging.getLogger(__name__)

GROQ_MAX_PROMPT_CHARS = 16_000
GROQ_TPM_BUDGET = 9_500
KEY_RATE_LIMIT_STRIKES = 3


class PayloadTooLargeError(Exception):
    pass


class AllKeysDead(Exception):
    pass


class _KeySlot:
    __slots__ = ("client", "label", "rl_hits", "dead")

    def __init__(self, client, label: str):
        self.client = client
        self.label = label
        self.rl_hits = 0
        self.dead = False

    def strike(self) -> bool:
        self.rl_hits += 1
        if self.rl_hits >= KEY_RATE_LIMIT_STRIKES:
            self.dead = True
        return self.dead


def _parse_retry_after(err_str: str) -> float:
    m = re.search(r'try again in ([\d.]+)\s*(ms|s)', err_str, re.IGNORECASE)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).lower()
    return (val / 1000.0) if unit == "ms" else val


class GroqThrottle:
    def __init__(self, rpm: int):
        self._min_gap = 60.0 / rpm
        self._window_start = time.monotonic()
        self._window_tokens = 0
        self._last_sent = 0.0

    def _reset_window_if_needed(self):
        if time.monotonic() - self._window_start >= 60.0:
            self._window_start = time.monotonic()
            self._window_tokens = 0

    def before(self, estimated_tokens: int):
        self._reset_window_if_needed()
        if self._window_tokens + estimated_tokens > GROQ_TPM_BUDGET:
            wait = 60.0 - (time.monotonic() - self._window_start)
            if wait > 0:
                log.info(f"        TPM budget at {self._window_tokens:,}/{GROQ_TPM_BUDGET:,} — pausing {wait:.1f}s")
                time.sleep(wait + 1.0)
            self._window_start = time.monotonic()
            self._window_tokens = 0
        gap = time.monotonic() - self._last_sent
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)

    def after_success(self, tokens_used: int):
        self._last_sent = time.monotonic()
        self._window_tokens += tokens_used
        if tokens_used > 5_000:
            extra = min(tokens_used / 1_000.0, 10.0)
            log.info(f"        heavy request ({tokens_used:,} tokens) — +{extra:.1f}s cooldown")
            time.sleep(extra)

    def after_rate_limit(self, err_str: str):
        parsed = _parse_retry_after(err_str)
        wait = (parsed + 5.0) if (parsed and parsed < 30.0) else 60.0
        log.warning(f"        waiting {wait:.1f}s before rotating to next key")
        time.sleep(wait)
        self._window_start = time.monotonic()
        self._window_tokens = 0
        self._last_sent = time.monotonic()

    def after_payload_too_large(self):
        log.warning("        payload too large — waiting 10s")
        time.sleep(10.0)
        self._last_sent = time.monotonic()


class _GeminiSlot:
    __slots__ = ("key", "model", "label", "rl_hits", "dead")

    def __init__(self, key: str, model_name: str, label: str):
        import google.generativeai as genai
        genai.configure(api_key=key)
        self.key = key
        self.model = genai.GenerativeModel(model_name)
        self.label = label
        self.rl_hits = 0
        self.dead = False

    def strike(self) -> bool:
        self.rl_hits += 1
        if self.rl_hits >= KEY_RATE_LIMIT_STRIKES:
            self.dead = True
        return self.dead


class AIClient:
    def __init__(self, cfg):
        self.provider = cfg["ai"]["provider"].lower()

        if self.provider == "gemini":
            import google.generativeai as genai
            keys = cfg["ai"].get("gemini_keys", [])
            model_name = cfg["ai"].get("gemini_model", "gemini-1.5-flash")
            self._gemini_slots = [_GeminiSlot(k, model_name, f"key {i+1}") for i, k in enumerate(keys)]
            self._gemini_cursor = 0
            log.info(f"AI: Gemini · {model_name} · {len(keys)} key(s)")

        elif self.provider == "groq":
            from groq import Groq
            keys = cfg["ai"].get("groq_keys", [])
            self.model_name = cfg["ai"].get("groq_model", "llama-3.3-70b-versatile")
            rpm = cfg["ai"].get("groq_rpm", 25)
            self._slots = [_KeySlot(Groq(api_key=k), f"key {i+1}") for i, k in enumerate(keys)]
            self._cursor = 0
            self._throttle = GroqThrottle(rpm)
            log.info(f"AI: Groq · {self.model_name} · {len(keys)} key(s) · {rpm} RPM")
        else:
            raise ValueError(f"Unknown AI provider '{self.provider}'")

    # ── Gemini helpers ────────────────────────────────────────────────────────

    def _live_gemini_slots(self) -> list:
        return [s for s in self._gemini_slots if not s.dead]

    def _next_gemini_slot(self) -> _GeminiSlot:
        live = self._live_gemini_slots()
        if not live:
            raise AllKeysDead(f"All {len(self._gemini_slots)} Gemini key(s) exhausted.")
        slot = live[self._gemini_cursor % len(live)]
        self._gemini_cursor += 1
        return slot

    def _generate_gemini(self, prompt: str) -> str:
        max_attempts = len(self._gemini_slots) * KEY_RATE_LIMIT_STRIKES

        for _ in range(max_attempts):
            slot = self._next_gemini_slot()
            try:
                import google.generativeai as genai
                genai.configure(api_key=slot.key)
                resp = slot.model.generate_content(prompt)
                content = resp.text
                if not content or not content.strip():
                    raise ValueError("Gemini returned empty content")
                return content

            except Exception as e:
                err = str(e).lower()
                is_rate_limit = "429" in err or "resource_exhausted" in err or "quota" in err or "too many" in err

                if is_rate_limit:
                    just_died = slot.strike()
                    live_count = len(self._live_gemini_slots())
                    if just_died:
                        log.warning(f"        Gemini {slot.label} marked dead. {live_count} key(s) remaining.")
                    else:
                        log.warning(f"        Gemini {slot.label} rate limit ({slot.rl_hits}/{KEY_RATE_LIMIT_STRIKES} strikes). {live_count} live.")
                    if live_count == 0:
                        raise AllKeysDead(f"All {len(self._gemini_slots)} Gemini key(s) exhausted.")
                    time.sleep(60.0)
                    continue
                raise

        raise RuntimeError(f"Gemini: exhausted {max_attempts} attempts")

    # ── Groq helpers ──────────────────────────────────────────────────────────

    def _live_slots(self) -> list:
        return [s for s in self._slots if not s.dead]

    def _next_live_slot(self) -> _KeySlot:
        live = self._live_slots()
        if not live:
            raise AllKeysDead(f"All {len(self._slots)} Groq key(s) exhausted.")
        slot = live[self._cursor % len(live)]
        self._cursor += 1
        return slot

    def _generate_groq(self, prompt: str) -> str:
        estimated_tokens = len(prompt) // 2
        self._throttle.before(estimated_tokens)
        max_attempts = len(self._slots) * KEY_RATE_LIMIT_STRIKES

        for _ in range(max_attempts):
            slot = self._next_live_slot()
            try:
                resp = slot.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                    temperature=0.3,
                )
                tokens_used = resp.usage.total_tokens if resp.usage else estimated_tokens
                self._throttle.after_success(tokens_used)
                content = resp.choices[0].message.content
                if not content or not content.strip():
                    raise ValueError("Groq returned empty content")
                return content

            except Exception as e:
                err = str(e).lower()
                if "413" in err or "payload too large" in err or "request too large" in err:
                    self._throttle.after_payload_too_large()
                    raise PayloadTooLargeError("Prompt too large for Groq (413)")

                if "429" in err or "rate_limit" in err or "too many" in err:
                    just_died = slot.strike()
                    live_count = len(self._live_slots())
                    if just_died:
                        log.warning(f"        {slot.label} marked dead. {live_count} key(s) remaining.")
                    else:
                        log.warning(f"        {slot.label} rate limit ({slot.rl_hits}/{KEY_RATE_LIMIT_STRIKES} strikes). {live_count} live.")
                    if live_count == 0:
                        raise AllKeysDead(f"All {len(self._slots)} Groq key(s) exhausted.")
                    self._throttle.after_rate_limit(str(e))
                    continue
                raise

        raise RuntimeError(f"Groq: exhausted {max_attempts} attempts")

    # ── Public ────────────────────────────────────────────────────────────────

    def generate(self, prompt: str) -> str:
        if self.provider == "gemini":
            return self._generate_gemini(prompt)
        return self._generate_groq(prompt)
