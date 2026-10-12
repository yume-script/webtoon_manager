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


def notify(cfg, title, description, color=COLOR_INFO, fields=None, mention_manage_tab=True,
           url=None, thumbnail=None):
    """cfg: {DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID}"""
    embed = {"title": title, "description": description, "color": color}
    if url:
        embed["url"] = url
    if thumbnail:
        embed["thumbnail"] = {"url": thumbnail}
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


def notify_finished(cfg, title, title_id, auto_unsubscribed=False):
    """auto_unsubscribed=True면 이미 자동으로 구독해제 처리됐다는 문구로 보낸다
    (설정 AUTO_UNSUBSCRIBE_ON_FINISH가 켜져 있을 때 pipeline.run_scan_finished가 전달)."""
    if auto_unsubscribed:
        description = (
            "titleId=%s 작품이 완결로 확인되어 자동으로 구독해제 처리했습니다. "
            "새 시즌 등으로 다시 연재되면 카테고리탭의 '전체 목록'에서 다시 구독해주세요." % title_id)
    else:
        description = (
            "titleId=%s 작품이 완결로 확인되었습니다. 카테고리탭의 '구독중' 목록에서 "
            "구독해제 또는 알람만 끄기를 선택해주세요." % title_id)
    return notify(cfg, "📗 완결 감지: %s" % title, description,
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


def notify_new_episodes(cfg, platform_label, title, episodes, url=None, thumbnail=None):
    """새 회차 알림. episodes: [(회차번호, 부제), ...] - 이번에 받은 새 회차들."""
    if not episodes:
        return False, "새 회차 없음"
    eps = sorted(episodes, key=lambda e: e[0])
    lines = []
    import re as _re
    for no, sub in eps[:10]:
        sub = (sub or "").strip()
        if title and sub.startswith(title):          # 카카오 부제는 "작품명 N화" 형태가 많음
            sub = sub[len(title):].strip(" -:·")
        if _re.fullmatch(r"%s\s*화" % no, sub or ""):  # 남은 게 회차 번호뿐이면 생략
            sub = ""
        lines.append("· %s화%s" % (no, (" - %s" % sub) if sub else ""))
    if len(eps) > 10:
        lines.append("· 외 %d화" % (len(eps) - 10))
    head = "%s %s" % (platform_label, title) if platform_label else title
    return notify(cfg, "🆕 %s - 새 회차 %d화" % (head, len(eps)), "\n".join(lines),
                  color=COLOR_DONE, mention_manage_tab=False, url=url, thumbnail=thumbnail)


def wants_new_episode_notice(cfg, t):
    """이 작품의 새 회차를 알릴지: 카드에서 🔔를 켠 작품, 또는 [공통] '구독 작품 전부 알림'이 켜진 경우."""
    if (t or {}).get("notify_new"):
        return True
    v = cfg.get("NOTIFY_NEW_EPISODES_ALL")
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
