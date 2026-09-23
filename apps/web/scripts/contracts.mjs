import { mkdir, readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";
import { compileFromFile } from "json-schema-to-typescript";
import { fileURLToPath } from "node:url";

const source = new URL("../../../contracts/chat/", import.meta.url);
const output = new URL("../src/chat/generated/", import.meta.url);
const results = {
  "http.ts": "// Generated from contracts/chat/openapi.json. Do not edit.\n" + astToString(await openapiTS(new URL("openapi.json", source))),
  "ws.d.ts": await compileFromFile(fileURLToPath(new URL("ws-server.schema.json", source)), {
    bannerComment: "// Generated from contracts/chat/ws-server.schema.json. Do not edit.",
  }),
};
if (!process.argv.includes("--check")) await mkdir(output, { recursive: true });
for (const [name, contents] of Object.entries(results)) {
  const target = new URL(name, output);
  if (process.argv.includes("--check")) {
    if (await readFile(target, "utf8") !== contents) throw new Error(`${name} is stale. Run pnpm contracts:generate.`);
  } else await writeFile(target, contents);
}
