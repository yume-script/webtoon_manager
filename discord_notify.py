# -*- coding: utf-8 -*-
import requests

COLOR_INFO = 0x3498DB
COLOR_WARN = 0xE67E22
COLOR_DONE = 0x2ECC71
COLOR_ERROR = 0xE74C3C


def _send_webhook(webhook_url, embed, timeout=10):
    if not webhook_url:
        return False, "웹훅 URL 없음"
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=timeout)
        if resp.status_code >= 300:
            return False, "웹훅 응답 코드 %s: %s" % (resp.status_code, resp.text[:200])
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def _send_bot_message(bot_token, channel_id, content, embed=None, timeout=10):
    if not bot_token or not channel_id:
        return False, "봇 토큰/채널ID 없음"
    url = "https://discord.com/api/v10/channels/%s/messages" % channel_id
    headers = {"Authorization": "Bot %s" % bot_token}
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code >= 300:
            return False, "봇 응답 코드 %s: %s" % (resp.status_code, resp.text[:200])
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def notify(cfg, title, description, color=COLOR_INFO, fields=None, mention_manage_tab=True):
    """cfg: {DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID}"""
    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()]
    if mention_manage_tab:
        embed.setdefault("footer", {"text": "BookOasis > 웹툰 다운로더 카테고리탭에서 확인/처리하세요."})

    ok_any = False
    errs = []
    webhook_url = cfg.get("DISCORD_WEBHOOK_URL")
    if webhook_url:
        ok, msg = _send_webhook(webhook_url, embed)
        ok_any = ok_any or ok
        if not ok:
            errs.append("webhook: %s" % msg)

    bot_token = cfg.get("DISCORD_BOT_TOKEN")
    channel_id = cfg.get("DISCORD_CHANNEL_ID")
    if bot_token and channel_id:
        ok, msg = _send_bot_message(bot_token, channel_id, content="", embed=embed)
        ok_any = ok_any or ok
        if not ok:
            errs.append("bot: %s" % msg)

    if not webhook_url and not (bot_token and channel_id):
        return False, "디스코드 알림 대상이 설정되지 않음"
    return ok_any, ("; ".join(errs) if errs else "ok")


def notify_finished(cfg, title, title_id):
    return notify(
        cfg, "📗 완결 감지: %s" % title,
        "titleId=%s 작품이 완결로 확인되었습니다. 카테고리탭의 '구독중' 목록에서 "
        "구독해제 또는 알람만 끄기를 선택해주세요." % title_id,
        color=COLOR_DONE, fields={"titleId": title_id})


def notify_cookie_expired(cfg):
    return notify(
        cfg, "🍪 네이버 쿠키 만료",
        "성인 인증 쿠키(로그인 세션)가 만료된 것으로 보입니다. "
        "플러그인 설정에서 쿠키(JSON)를 새로 발급해 넣어주세요.",
        color=COLOR_WARN)


def notify_failures(cfg, failures):
    if not failures:
        return True, "no failures"
    lines = ["- %s (titleId=%s, no=%s): %s" % (f.get("title"), f.get("title_id"),
                                                  f.get("episode_no"), f.get("error"))
              for f in failures[:20]]
    more = "" if len(failures) <= 20 else "\n...외 %d건" % (len(failures) - 20)
    return notify(
        cfg, "⚠️ 다운로드 실패 요약 (%d건)" % len(failures),
        "\n".join(lines) + more, color=COLOR_ERROR)
