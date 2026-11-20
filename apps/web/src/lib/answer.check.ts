import { readFile, readdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { toAnswerModel } from './answer';
import { toProcessModel } from './runProcess';
import { isRecord } from './guards';

const fixtureDir = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../contracts/fixtures');
const fixturePattern =
  /^page-a-(answered-now-b5|abstained-.*|abstain-.*|tier-only-aapl-2026-03-24|run-failed|run-cancelled|partial-legacy|old-run-no-trace|grade-.*|cost-unavailable)\.json$/;
const internalCodePattern = /[A-Z]{2,}_[A-Z_]+/;
const fixtureOrder = [
  'answered-now-b5',
  'abstained-',
  'abstain-',
  'tier-only-aapl-2026-03-24',
  'run-failed',
  'run-cancelled',
  'partial-legacy',
  'old-run-no-trace',
  'grade-',
  'cost-unavailable',
];

function stringsIn(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(stringsIn);
  // traceJson and legacyText are shown verbatim on purpose (copy button, old runs).
  if (isRecord(value))
    return Object.entries(value)
      .filter(([key]) => key !== 'traceJson' && key !== 'legacyText')
      .flatMap(([, item]) => stringsIn(item));
  return [];
}

async function main(): Promise<void> {
  const fixtureNames = (await readdir(fixtureDir))
    .filter((name) => fixturePattern.test(name))
    .sort((left, right) => {
      const rank = (name: string) =>
        fixtureOrder.findIndex((prefix) => name.slice('page-a-'.length).startsWith(prefix));
      return rank(left) - rank(right) || left.localeCompare(right);
    });
  const answerModulePath = resolve(dirname(fileURLToPath(import.meta.url)), 'answer.ts');
  const processModulePath = resolve(dirname(fileURLToPath(import.meta.url)), 'runProcess.ts');
  const checkModulePath = fileURLToPath(import.meta.url);

  for (const name of fixtureNames) {
    try {
      const fixture: unknown = JSON.parse(await readFile(resolve(fixtureDir, name), 'utf8'));
      const response = isRecord(fixture) ? fixture.response : null;
      if (!isRecord(response)) throw new Error('fixture has no response object');
      const answer = toAnswerModel(response as Parameters<typeof toAnswerModel>[0]);
      const process = toProcessModel(response as Parameters<typeof toProcessModel>[0]);
      const suspicious = [...stringsIn(answer), ...stringsIn(process)].filter((text) =>
        internalCodePattern.test(text),
      );
      const decisions = process.decisionSummary ?? 'none';
      const checks = process.quoteSummary ?? 'none';
      console.log(
        `${name}: ${answer.outcome.label}; quotes ${answer.quotes.length}; gaps ${answer.gaps.length}; stages ${process.stages.length}; decisions ${decisions}; quote checks ${checks}`,
      );
      for (const text of suspicious) console.log(`${name}: ERROR internal code in model string: ${text}`);
    } catch (error) {
      console.log(`${name}: ERROR ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

void main();
