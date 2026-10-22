/* Theme-Steuerung für Dashboard und Maintenance.
 * Modi: "auto" (hell zwischen Sonnenauf- und -untergang, sonst dunkel), "light", "dark".
 * Sonnenstand wird offline berechnet (NOAA/SunCalc-Algorithmus) – kein Internet nötig. */
(function (global) {
  "use strict";
  const PI = Math.PI, rad = PI / 180, DAY = 864e5, J1970 = 2440588, J2000 = 2451545, J0 = 0.0009;
  const toDays = (d) => d.valueOf() / DAY - 0.5 + J1970 - J2000;
  const fromJulian = (j) => new Date((j + 0.5 - J1970) * DAY);

  function sunTimes(date, lat, lng) {
    const lw = rad * -lng, phi = rad * lat, e = rad * 23.4397;
    const d = toDays(date);
    const n = Math.round(d - J0 - lw / (2 * PI));
    const approx = (Ht) => J0 + (Ht + lw) / (2 * PI) + n;
    const ds = approx(0);
    const M = rad * (357.5291 + 0.98560028 * ds);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + PI;
    const dec = Math.asin(Math.sin(L) * Math.sin(e));
    const transit = (a) => J2000 + a + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
    const jnoon = transit(ds);
    const cosW = (Math.sin(-0.833 * rad) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec));
    if (cosW > 1) return { polar: "night" };   // Polarnacht
    if (cosW < -1) return { polar: "day" };    // Mitternachtssonne
    const jset = transit(approx(Math.acos(cosW)));
    return { sunrise: fromJulian(jnoon - (jset - jnoon)), sunset: fromJulian(jset), polar: null };
  }

  function isNight(now, lat, lon) {
    const noon = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12);
    const t = sunTimes(noon, lat, lon);
    if (t.polar) return t.polar === "night";
    return now < t.sunrise || now >= t.sunset;
  }

  const Theme = {
    mode: "auto", lat: 49.6935, lon: 8.64841, timer: null, listeners: [],
    sunTimes, isNight,
    resolve(now) {
      if (this.mode === "dark") return "dark";
      if (this.mode === "light") return "light";
      return isNight(now || new Date(), this.lat, this.lon) ? "dark" : "light";
    },
    apply() {
      const t = this.resolve();
      if (document.documentElement.dataset.theme !== t) {
        document.documentElement.dataset.theme = t;
        this.listeners.forEach((fn) => { try { fn(t); } catch (_) {} });
      }
      return t;
    },
    set(opts) {
      if (opts.mode) this.mode = opts.mode;
      if (opts.station) { this.lat = opts.station.lat; this.lon = opts.station.lon; }
      try { localStorage.setItem("fw-theme", JSON.stringify({ mode: this.mode, lat: this.lat, lon: this.lon })); } catch (_) {}
      this.apply();
    },
    onChange(fn) { this.listeners.push(fn); },
    init() {
      try {  // letzten Stand aus dem Browser nehmen, damit beim Laden nichts aufblitzt
        const saved = JSON.parse(localStorage.getItem("fw-theme") || "null");
        if (saved) { this.mode = saved.mode; this.lat = saved.lat; this.lon = saved.lon; }
      } catch (_) {}
      this.apply();
      this.timer = setInterval(() => this.apply(), 30000);   // Auto-Umschaltung zur Sonnenzeit
    },
  };
  Theme.init();
  global.Theme = Theme;
})(window);
