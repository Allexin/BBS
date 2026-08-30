# BBS development

Test credentials belong only in the repository-local `secrets` directory, which is
ignored by Git. Use one strict file per integration, for example
`secrets\telegram.json`. Tests must never read or modify credentials below a Stable
deployment.

`secrets\telegram.json` supports direct, HTTP proxy, and SOCKS5 transport:

```json
{
  "bot_token": "test credential",
  "chat_id": "test destination",
  "message_thread_id": null,
  "proxy_url": null
}
```

Set `message_thread_id` to a positive integer for a forum topic. Set `proxy_url` to
an HTTP(S) or SOCKS5 URL when the integration requires a proxy.
