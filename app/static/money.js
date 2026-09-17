"use strict";
// Presentation only. API Decimal values stay as strings; never convert money to Number.
const MoneyDisplay = Object.freeze({
  format(value, currency) {
    if (value == null) return "Unrated";
    if (currency !== "VND") return String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
    if (typeof value === "number") throw new TypeError("VND must be a decimal string");
    const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(value));
    if (!match) throw new TypeError("Invalid VND decimal string");
    let whole = BigInt(match[2]);
    // ROUND_HALF_UP: nearest integer, ties away from zero, including credits.
    if ((match[3] || "").charAt(0) >= "5") whole += 1n;
    const sign = match[1] === "-" && whole !== 0n ? "-" : "";
    return sign + whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",") + " ₫";
  }
});
