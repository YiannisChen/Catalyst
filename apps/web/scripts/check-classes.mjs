// Flags class names the design tokens cannot style: spacing steps above 7 or fractional, and arbitrary [..] values.
// Tailwind's default scale is reset to our tokens, so such classes silently produce no CSS.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const spacing =
  /(?:^|[\s"'`:])-?(?:p[trblxy]?|m[trblxy]?|gap(?:-[xy])?|space-[xy]|inset(?:-[xy])?|top|right|bottom|left|[hw]|min-[hw]|max-[hw]|size)-(\d+(?:\.\d+)?)(?=[\s"'`]|$)/g;
const arbitrary = /(?:^|[\s"'`:])[a-z-]+-\[[^\]\s]+\]/g;
const files = [];
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (/\.tsx$/.test(name)) files.push(path);
  }
};
walk('src');
let problems = 0;
for (const file of files) {
  readFileSync(file, 'utf8')
    .split('\n')
    .forEach((line, index) => {
      if (!/className|cn\(|cva\(/.test(line)) return;
      const bad = [...line.matchAll(spacing)]
        .filter((m) => Number(m[1]) > 7 || !Number.isInteger(Number(m[1])))
        .map((m) => m[0].trim());
      bad.push(...[...line.matchAll(arbitrary)].map((m) => m[0].trim()));
      if (bad.length) {
        problems += bad.length;
        console.log(`${file}:${index + 1}: ${bad.join(' ')}`);
      }
    });
}
if (problems) {
  console.log(`${problems} class names have no token behind them`);
  process.exit(1);
}
