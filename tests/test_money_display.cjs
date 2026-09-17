const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const display = vm.runInNewContext(fs.readFileSync(path.join(root, 'app/static/money.js'), 'utf8')+'; MoneyDisplay');

test('VND rounds half up with exact strings, including credits and huge amounts', () => {
  for (const [raw, expected] of [
    ['2020858.40310001', '2,020,858 ₫'],
    ['2020858.60310001', '2,020,859 ₫'],
    ['2.50000000', '3 ₫'], ['-2.50000000', '-3 ₫'],
    ['999.99999999', '1,000 ₫'], ['0.499999999999', '0 ₫'],
    ['0.50000000', '1 ₫'], ['-0.00000001', '0 ₫'], ['0', '0 ₫'],
    ['9007199254740993.50000000', '9,007,199,254,740,994 ₫']
  ]) {
    const actual = display.format(raw, 'VND');
    assert.equal(actual, expected);
    assert.match(actual, /^-?\d{1,3}(,\d{3})* ₫$/);
  }
});
test('Presentation preserves currency semantics and rejects binary-float input', () => {
  assert.equal(display.format(null, 'VND'), 'Unrated');
  assert.equal(display.format('1.23450000', 'USD'), '1.2345');
  assert.throws(() => display.format(2.5, 'VND'));
  assert.throws(() => display.format('garbage', 'VND'));
});
test('Every monetary dashboard loads the formatter before rendering', () => {
  for (const [html, script] of [['index.html','app.js'],['costs.html','costs.js'],['billing.html','billing.js']]) {
    const page=fs.readFileSync(path.join(root,'app/static',html),'utf8');
    assert.ok(page.indexOf('/static/money.js')>=0);
    assert.ok(page.indexOf('/static/money.js')<page.indexOf('/static/'+script));
    assert.match(fs.readFileSync(path.join(root,'app/static',script),'utf8'),/MoneyDisplay\.format/);
  }
});
