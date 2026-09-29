import { weatherAgent } from "./agent.ts";

async function main() {
  const response = await weatherAgent.generate("What's the weather in Lisbon right now?");
  console.log(response.text);
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
