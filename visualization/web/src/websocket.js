// WebSocket subscriber with automatic reconnect (exponential back-off 0.5 s .. 5 s).
// Read-only: the only message sent is {"type": "ping"} (the server answers "pong").

export class WsClient {
  constructor(url, { onMessage, onState, minDelay = 500, maxDelay = 5000, pingMs = 5000, WebSocketImpl } = {}) {
    this.url = url;
    this.onMessage = onMessage || (() => {});
    this.onState = onState || (() => {});
    this.minDelay = minDelay;
    this.maxDelay = maxDelay;
    this.delay = minDelay;
    this.pingMs = pingMs;
    this.WS = WebSocketImpl || globalThis.WebSocket;
    this.ws = null;
    this.state = "idle";
    this.stopped = false;
    this.timer = null;
    this.pinger = null;
    this.parseErrors = 0;
  }

  setState(s, detail) {
    this.state = s;
    this.onState(s, detail);
  }

  connect() {
    this.stopped = false;
    this.setState("connecting");
    let ws;
    try {
      ws = new this.WS(this.url);
    } catch (e) {
      this.scheduleReconnect(String(e));
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.delay = this.minDelay;
      this.setState("open");
      this.pinger = setInterval(() => {
        if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
      }, this.pingMs);
    };
    ws.onmessage = (ev) => {
      let m;
      try {
        m = JSON.parse(ev.data);
      } catch (e) {
        this.parseErrors += 1;
        return;
      }
      this.onMessage(m);
    };
    ws.onclose = (ev) => {
      clearInterval(this.pinger);
      if (this.ws === ws) this.ws = null;
      if (this.stopped) this.setState("closed", ev.reason);
      else this.scheduleReconnect(ev.reason || `code ${ev.code}`);
    };
    ws.onerror = () => {};               // onclose follows
  }

  scheduleReconnect(reason) {
    this.setState("reconnecting", `${reason}; retry in ${(this.delay / 1000).toFixed(1)} s`);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), this.delay);
    this.delay = Math.min(this.maxDelay, this.delay * 2);
  }

  close() {
    this.stopped = true;
    clearTimeout(this.timer);
    clearInterval(this.pinger);
    if (this.ws) this.ws.close();
    else this.setState("closed");
  }
}
