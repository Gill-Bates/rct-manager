//
// app/admin/static/js/admin-chart-axis.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Shared category axis of the Spot Market and Weather day charts. A slot is identified by its
// local wall-clock time plus an occurrence number ("02:00#1", "02:00#2"), never by array index, so
// a DST day keeps every value under its real time: the repeated hour of a 100-interval day gets
// its own extra slots and the skipped hour of a 92-interval day stays empty.

// days: arrays of points carrying a local `label` ("HH:MM"). Returns the axis `labels` and, per
// day, the slot index of each point (`slots[day][pointIndex]`).
function buildDayAxis(days) {
  const hhmm = (index) => `${String(Math.floor(index / 4)).padStart(2, '0')}:${String((index % 4) * 15).padStart(2, '0')}`;
  const keys = Array.from({ length: 96 }, (_, index) => `${hhmm(index)}#1`);
  const keyed = days.map((day) => {
    const seen = new Map();
    return day.map((point) => {
      const n = (seen.get(point.label) || 0) + 1;
      seen.set(point.label, n);
      return `${point.label}#${n}`;
    });
  });
  // An unknown key (repeated hour, off-grid minute) goes right after its predecessor in its day.
  for (const dayKeys of keyed) {
    dayKeys.forEach((key, index) => {
      if (keys.includes(key)) return;
      const previous = index > 0 ? keys.indexOf(dayKeys[index - 1]) : -1;
      keys.splice(previous + 1, 0, key);
    });
  }
  const labels = keys.map((key) => {
    const [time, n] = key.split('#');
    return n === '1' ? time : `${time} (${n})`;
  });
  return { labels, slots: keyed.map((dayKeys) => dayKeys.map((key) => keys.indexOf(key))) };
}
