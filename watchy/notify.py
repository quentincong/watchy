"""Telegram Bot notifications for Watchy.

Pushes natural-language summaries on: signal fired, pipeline result, errors.
Also sends the full markdown analysis report as a document attachment.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Telegram rejects messages over 4096 chars. Keep a margin below the hard cap.
TELEGRAM_MAX = 4096
_WORKING_LIMIT = 4000


def _split_message(text: str, limit: int = _WORKING_LIMIT) -> list[str]:
    """Split a message into Telegram-sized chunks without breaking HTML tags.

    Splits on newline boundaries: each source line carries balanced HTML tags
    (open+close on the same line), so accumulating whole lines preserves tag
    integrity. A single line longer than *limit* (e.g. the advisor detail
    paragraph — plain escaped text, no tags) is hard-split on whitespace.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            # flush whatever's accumulated, then hard-split the oversized line
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(line, limit))
            continue
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _hard_split(line: str, limit: int) -> list[str]:
    """Split a single long line on whitespace, falling back to a hard cut."""
    chunks: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = word if not current else current + " " + word
        if len(candidate) > limit:
            if current:
                chunks.append(current)
                current = ""
            # a single word longer than the limit — cut it
            while len(word) > limit:
                chunks.append(word[:limit])
                word = word[limit:]
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            logger.warning("Telegram not configured — notifications disabled")

    def send(self, message: str) -> bool:
        """Send a message, splitting into ≤4096-char chunks. Returns True if all sent."""
        if not self._enabled:
            logger.info("[telegram would send]: %s", message)
            return False
        chunks = _split_message(message)
        return all(
            self._post("sendMessage", {"text": chunk, "parse_mode": "HTML"})
            for chunk in chunks
        )

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters so Telegram parse_mode=HTML doesn't choke."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def send_document(
        self,
        file_path: str,
        *,
        caption: str | None = None,
    ) -> bool:
        """Send a document file via Telegram.

        Args:
            file_path: Path to the file to send.
            caption: Optional caption text.

        Returns:
            True on success.
        """
        if not self._enabled:
            logger.info(
                "[telegram would send_document]: %s (caption=%s)", file_path, caption
            )
            return False
        return self._post_file("sendDocument", file_path, caption=caption)

    def signal_fired(
        self,
        ticker: str,
        signal_type: str,
        indicators: dict[str, Any],
        triggered_analysts: list[str],
    ) -> bool:
        """Notify that a technical signal has fired."""
        label = _signal_label(signal_type)
        price = indicators.get("current_price", "N/A")
        rsi = indicators.get("rsi", "N/A")
        stage = indicators.get("sepa_stage", "?")

        lines = [
            f"<b>Signal Fired</b> — ${ticker}",
            f"<b>Signal:</b> {label}",
            f"<b>Price:</b> ${price}",
            f"<b>RSI:</b> {rsi}",
            f"<b>SEPA Stage:</b> {_stage_name(stage)}",
        ]
        if triggered_analysts:
            lines.append(f"<b>Analysts launching:</b> {', '.join(triggered_analysts)}")

        return self.send("\n".join(lines))

    def rescan_capped(
        self,
        ticker: str,
        signal_type: str,
        indicators: dict[str, Any],
        runs_today: int,
        cap: int,
    ) -> bool:
        """Notify that a signal fired but the paid Tier 1 rescan was skipped (daily cap)."""
        label = _signal_label(signal_type)
        price = indicators.get("current_price", "N/A")
        return self.send(
            "\n".join([
                f"<b>Signal Fired (rescan capped)</b> — ${ticker}",
                f"<b>Signal:</b> {label}",
                f"<b>Price:</b> ${price}",
                f"<b>Skipped:</b> {runs_today}/{cap} Tier 1 rescans already run today — "
                f"no LLM pipeline launched to save cost.",
            ])
        )

    def pipeline_result(
        self,
        ticker: str,
        signal_type: str,
        result: dict[str, Any],
        *,
        position_text: str | None = None,
        advice: dict[str, str] | None = None,
        plan_card: str | None = None,
    ) -> bool:
        """Notify with a natural-language pipeline summary + full report file.

        If position_text and advice are provided, includes position context and
        a specific recommendation on whether to alter the position.

        When a ``report_path`` is present in *result*, the full markdown report
        is sent as a document attachment.
        """
        esc = self._escape_html
        verdict = result.get("verdict", "")
        analyst_count = result.get("analyst_count")

        lines = [
            f"<b>Analysis Complete</b> — ${ticker}",
            f"<b>Trigger:</b> {_signal_label(signal_type)}",
        ]
        # Headline verdict line (#3): one-word BUY/SELL/HOLD + how many analysts ran.
        # The Trader Plan + Risk/Final Call blocks are intentionally NOT inlined —
        # they (and the raw analyst reports) live in the attached .md. The message
        # is a one-glance headline; open the report for the detail.
        if verdict:
            icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(verdict, "")
            suffix = f" ({analyst_count} analysts)" if analyst_count else ""
            lines.append(f"<b>Verdict:</b> {icon} <b>{esc(verdict)}</b>{suffix}")
        else:
            # Sparse pipelines (e.g. market-only Tier 1) may have no verdict; fall
            # back to the summary so the message isn't a bare header.
            summary = (result.get("summary") or "").strip()
            if summary:
                lines += ["", "<b>Summary</b>", esc(summary)]

        ok = self.send("\n".join(lines))

        # Position context + advisor synthesis go in a SEPARATE message. It's a
        # different source (the Gemini advisor, not the analyst team), and keeping
        # it apart stops the advice paragraph from being chunk-split across the
        # analysis text.
        advice_lines: list[str] = []
        if position_text:
            advice_lines.append(f"<b>Your Position</b> — ${ticker}")
            advice_lines.append(esc(position_text))
        if advice:
            decision = esc(advice.get("decision", "?"))
            urgency = advice.get("urgency", "")
            detail = esc(advice.get("detail", ""))

            urgency_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(urgency, "")
            if advice_lines:
                advice_lines.append("")
            advice_lines.append(
                f"<b>Position Advice</b> — ${ticker}: {urgency_icon} <b>{decision}</b>"
                + (f" ({urgency} urgency)" if urgency else "")
            )
            take_profit = advice.get("take_profit", "")
            if _has_take_profit(take_profit):
                advice_lines.append(f"<b>💰 Take-Profit:</b> {esc(take_profit)}")
            if detail:
                advice_lines.append(detail)
        if plan_card:
            # Watchy 2.0 weekly plan card (already escaped by watchy.messages).
            if advice_lines:
                advice_lines.append("")
            advice_lines.append(plan_card)
        if advice_lines:
            ok = self.send("\n".join(advice_lines)) and ok

        # Send the full markdown report as a document
        report_path = result.get("report_path")
        if report_path and os.path.isfile(report_path):
            caption = f"📄 Full analysis report: ${ticker} — {_signal_label(signal_type)}"
            self.send_document(report_path, caption=caption)

        return ok

    def take_profit_alert(
        self,
        ticker: str,
        gain_pct: float,
        advice: dict[str, str] | None,
        position_text: str | None = None,
    ) -> bool:
        """Notify that a held winner crossed the take-profit floor intraday (#28).

        Fired by the Tier 1 zone-entry trigger. Surfaces the advisor's decision
        and — the point of the feature — the concrete sell-limit + whole-share
        count to pre-place so an intraday high banks the gain.
        """
        esc = self._escape_html
        lines = [
            f"<b>💰 Take-Profit Zone</b> — ${ticker}",
            f"Unrealized gain <b>+{gain_pct:.1f}%</b> crossed the take-profit floor "
            f"— set a sell-limit so a spike doesn't round-trip.",
        ]
        if position_text:
            lines += ["", esc(position_text)]
        if advice:
            decision = esc(advice.get("decision", "?"))
            urgency = advice.get("urgency", "")
            urgency_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(urgency, "")
            lines += [
                "",
                f"<b>Advice</b> — ${ticker}: {urgency_icon} <b>{decision}</b>"
                + (f" ({urgency} urgency)" if urgency else ""),
            ]
            take_profit = advice.get("take_profit", "")
            if _has_take_profit(take_profit):
                lines.append(f"<b>Sell-limit:</b> {esc(take_profit)}")
            detail = esc(advice.get("detail", ""))
            if detail:
                lines.append(detail)
        return self.send("\n".join(lines))

    def weekly_plan_failures(self, tickers: list[str], valid_count: int) -> bool:
        """One alert per Weekly Full batch listing tickers without a valid plan.

        Their previous plans are not extended: once last week's expiry passes,
        Tier 1 treats them as expired and entry guidance is information-only.
        """
        names = ", ".join(self._escape_html(t) for t in tickers)
        return self.send(
            "<b>⚠ Weekly Plan Refresh Incomplete</b>\n"
            f"<b>No valid plan:</b> {names}\n"
            f"<b>Valid plans:</b> {valid_count}\n"
            "Previous plans are NOT extended — these tickers get information-only "
            "entry guidance until a weekly run succeeds. Risk and take-profit "
            "monitoring continue. Force a rerun with "
            "<code>scripts/watchy_ctl.py weekly TICKER --yes</code> (paid)."
        )

    def advisor_failed(self, ticker: str, context: str) -> bool:
        """Notify that the advisor call failed for a ticker (#28 follow-up).

        get_advice swallows its own exception and returns None, so a failure used
        to be completely silent: the pipeline still "succeeded", no advice card
        was sent, and the #16 derived-target write was skipped. Three of these
        went unnoticed for a day on 2026-08-07. Surfacing it costs one short
        message and makes the gap visible the same day.
        """
        return self.send(
            f"<b>⚠ Advisor Failed</b> — ${self._escape_html(ticker)}\n"
            f"<b>Context:</b> {self._escape_html(context)}\n"
            "The analysis ran, but no position advice could be synthesized "
            "(and no target was derived for the proximity gate). See the journal "
            "for the traceback."
        )

    def error(self, context: str, error: Exception) -> bool:
        """Notify on critical errors."""
        return self.send(
            f"<b>⚠ Watchy Error</b>\n"
            f"<b>Context:</b> {context}\n"
            f"<b>Error:</b> {type(error).__name__}: {error}"
        )

    def _post(self, method: str, payload: dict[str, Any]) -> bool:
        import json
        import urllib.error
        import urllib.request

        # chat_id is required by the Telegram API on every send; inject it here
        # so all callers (send/signal_fired/error) get it without repeating it.
        payload = {"chat_id": self.chat_id, **payload}
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                if not body.get("ok"):
                    logger.error("Telegram API error: %s", body)
                    return False
                return True
        except urllib.error.HTTPError as exc:
            # Surface Telegram's error description (it's in the response body).
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            logger.error(
                "Telegram send failed: HTTP %s %s — %s", exc.code, exc.reason, detail,
            )
            return False
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False

    def _post_file(
        self,
        method: str,
        file_path: str,
        *,
        caption: str | None = None,
    ) -> bool:
        """Send a multipart/form-data request with a file attachment."""
        import json
        import urllib.error
        import urllib.request

        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        boundary = uuid4().hex
        filename = os.path.basename(file_path)

        # Guess MIME type
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        with open(file_path, "rb") as fh:
            file_bytes = fh.read()

        CRLF = b"\r\n"

        def _part(name: str, value: bytes, *, extra_headers: str = "") -> bytes:
            hdr = f'Content-Disposition: form-data; name="{name}"'
            if extra_headers:
                hdr += f"; {extra_headers}"
            return CRLF.join([
                f"--{boundary}".encode(),
                hdr.encode(),
                b"",
                value,
            ])

        parts: list[bytes] = []

        # chat_id
        parts.append(_part("chat_id", self.chat_id.encode()))

        # document
        file_part_headers = f'filename="{filename}"\r\nContent-Type: {mime_type}'
        parts.append(_part("document", file_bytes, extra_headers=file_part_headers))

        # optional caption
        if caption:
            parts.append(_part("caption", caption.encode("utf-8")))

        # closing
        parts.append(f"--{boundary}--".encode())

        body = CRLF.join(parts)

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = json.loads(resp.read())
                if not resp_body.get("ok"):
                    logger.error("Telegram API error (sendDocument): %s", resp_body)
                    return False
                return True
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            logger.error(
                "Telegram sendDocument failed: HTTP %s %s — %s",
                exc.code, exc.reason, detail,
            )
            return False
        except Exception:
            logger.exception("Failed to send Telegram document")
            return False


def _has_take_profit(value: str | None) -> bool:
    """True when the advisor's Take-Profit field carries an actionable sell-limit."""
    if not value:
        return False
    return value.strip().upper().rstrip(".") not in ("", "N/A", "NA", "NONE")


def _signal_label(signal_type: str) -> str:
    labels: dict[str, str] = {
        "golden_cross": "Golden Cross (50MA ↑ 200MA)",
        "death_cross": "Death Cross (50MA ↓ 200MA)",
        "rsi_oversold": "RSI Oversold (< 30)",
        "rsi_overbought": "RSI Overbought (> 70)",
        "macd_bullish_cross": "MACD Bullish Crossover",
        "macd_bearish_cross": "MACD Bearish Crossover",
        "bollinger_upper_breach": "Bollinger Upper Band Breach",
        "bollinger_lower_breach": "Bollinger Lower Band Breach",
        "volume_anomaly_strong": "Volume Anomaly (≥ 2x avg)",
        "atr_spike": "ATR Spike (≥ 1.5x avg)",
        "take_profit_zone": "Take-Profit Zone Entered",
        "scheduled_daily": "Scheduled Daily Run",
        "weekly_full": "Weekly Full Analysis",
    }
    return labels.get(signal_type, signal_type)


def _stage_name(stage: int | None) -> str:
    names = {1: "Basing", 2: "Advancing", 3: "Topping", 4: "Declining"}
    return names.get(stage, "?") if stage is not None else "?"
