import { z } from "zod";
import { getBrandLookup } from "@/server/features/ai-search/services/brandLookup";
import { explorePrompt } from "@/server/features/ai-search/services/promptExplorer";
import { buildProjectMeta } from "@/server/mcp/context";
import { mcpResponse } from "@/server/mcp/formatters";
import { optionalMetaOutputSchema } from "@/server/mcp/output-schemas";
import { withMcpProjectAuth } from "@/server/mcp/project-auth";
import {
  languageCodeSchema,
  locationCodeSchema,
  projectIdSchema,
} from "@/server/mcp/schemas";
import { resolveMarket } from "@/shared/keyword-locations";
import {
  BRAND_LOOKUP_MAX_INPUT_LENGTH,
  brandLookupResultSchema,
  PROMPT_EXPLORER_MAX_PROMPT_LENGTH,
  promptExplorerModelSchema,
  promptExplorerResultSchema,
  webSearchCountryCodeSchema,
} from "@/types/schemas/ai-search";
import { researchScopeSchema } from "@/shared/researchScope";

const brandVisibilityInputSchema = {
  projectId: projectIdSchema,
  query: z
    .string()
    .trim()
    .min(1)
    .max(BRAND_LOOKUP_MAX_INPUT_LENGTH)
    .describe(
      "Brand name, domain, or URL whose AI visibility should be analyzed.",
    ),
  competitors: z
    .array(z.string().trim().min(1).max(BRAND_LOOKUP_MAX_INPUT_LENGTH))
    .max(5)
    .optional()
    .describe("Up to 5 competing brands or domains for Share of Voice."),
  scope: researchScopeSchema
    .optional()
    .describe("Optional domain, subdomain, subfolder, or exact-URL scope."),
  locationCode: locationCodeSchema.optional(),
  languageCode: languageCodeSchema.optional(),
} as const;

type BrandVisibilityArgs = z.infer<
  z.ZodObject<typeof brandVisibilityInputSchema>
>;

const brandVisibilityOutputSchema = brandLookupResultSchema.extend(
  optionalMetaOutputSchema,
);

export const getAiBrandVisibilityTool = {
  name: "get_ai_brand_visibility",
  config: {
    title: "Get AI brand visibility",
    description:
      "Analyze a brand, domain, or URL in AI-generated search answers. Returns mention totals, ChatGPT and Google AI Overview platform breakdowns, cited top pages, top queries, and optional competitor Share of Voice. Reuses the AI Search brand lookup and its daily cache. Charges credits when not cached.",
    inputSchema: brandVisibilityInputSchema,
    outputSchema: brandVisibilityOutputSchema,
    annotations: {
      readOnlyHint: false,
      openWorldHint: true,
      destructiveHint: false,
    },
  },
  handler: withMcpProjectAuth(async (args: BrandVisibilityArgs, context) => {
    const { locationCode, languageCode } = resolveMarket(args, context.project);
    const result = await getBrandLookup(
      {
        projectId: args.projectId,
        query: args.query,
        competitors: args.competitors ?? [],
        scope: args.scope,
        locationCode,
        languageCode,
      },
      context.billing,
    );

    return mcpResponse({
      text: formatBrandVisibility(result),
      meta: buildProjectMeta(
        context,
        args.projectId,
        `/p/${args.projectId}/brand-lookup`,
        { q: args.query },
      ),
      structuredContent: result,
    });
  }),
};

const promptExplorerInputSchema = {
  projectId: projectIdSchema,
  prompt: z
    .string()
    .trim()
    .min(1)
    .max(PROMPT_EXPLORER_MAX_PROMPT_LENGTH)
    .describe("Prompt to run across the selected AI models."),
  models: z
    .array(promptExplorerModelSchema)
    .min(1)
    .max(4)
    .describe("One or more of: chat_gpt, claude, gemini, perplexity."),
  highlightBrand: z
    .string()
    .trim()
    .min(1)
    .max(BRAND_LOOKUP_MAX_INPUT_LENGTH)
    .optional()
    .describe(
      "Brand or domain to detect in answer text and citations. Omit to skip brand detection.",
    ),
  webSearch: z
    .boolean()
    .optional()
    .describe("Allow models to search the web. Defaults to true."),
  webSearchCountryCode: webSearchCountryCodeSchema
    .optional()
    .describe("Optional ISO-2 country used to localize model web search."),
} as const;

type PromptExplorerArgs = z.infer<
  z.ZodObject<typeof promptExplorerInputSchema>
>;

const promptExplorerOutputSchema = promptExplorerResultSchema.extend(
  optionalMetaOutputSchema,
);

export const exploreAiPromptTool = {
  name: "explore_ai_prompt",
  config: {
    title: "Explore an AI prompt",
    description:
      "Run one prompt across selected ChatGPT, Claude, Gemini, and Perplexity models. Returns each model's answer, exact model name, brand presence, citations and URLs, and fan-out queries. Reuses the AI Search prompt explorer and its 7-day per-model cache. Charges credits when not cached.",
    inputSchema: promptExplorerInputSchema,
    outputSchema: promptExplorerOutputSchema,
    annotations: {
      readOnlyHint: false,
      openWorldHint: true,
      destructiveHint: false,
    },
  },
  handler: withMcpProjectAuth(async (args: PromptExplorerArgs, context) => {
    const result = await explorePrompt(
      {
        projectId: args.projectId,
        prompt: args.prompt,
        models: args.models,
        highlightBrand: args.highlightBrand,
        webSearch: args.webSearch ?? true,
        webSearchCountryCode: args.webSearchCountryCode,
      },
      context.billing,
    );

    return mcpResponse({
      text: formatPromptExplorer(result),
      meta: buildProjectMeta(
        context,
        args.projectId,
        `/p/${args.projectId}/prompt-explorer`,
        { q: args.prompt },
      ),
      structuredContent: result,
    });
  }),
};

function formatBrandVisibility(
  result: z.infer<typeof brandLookupResultSchema>,
): string {
  const lines = [
    `AI visibility for ${result.resolvedTarget}`,
    `Total mentions: ${result.totalMentions ?? "unavailable"}`,
    `Total AI search volume: ${result.totalAiSearchVolume ?? "unavailable"}`,
    "",
    "Platform breakdown:",
    ...result.perPlatform.map(
      (platform) =>
        `- ${platform.platform}: ${platform.status}; mentions ${platform.mentions ?? "unavailable"}; AI search volume ${platform.aiSearchVolume ?? "unavailable"}`,
    ),
  ];

  if (result.topPages.length > 0) {
    lines.push(
      "",
      "Top cited pages:",
      ...result.topPages.map(
        (page) =>
          `- ${page.url} (${page.platform}; mentions ${page.mentions ?? "unavailable"})`,
      ),
    );
  }
  if (result.topQueries.length > 0) {
    lines.push(
      "",
      "Top queries:",
      ...result.topQueries.map(
        (query) =>
          `- [${query.platform}] ${query.question} (volume ${query.aiSearchVolume ?? "unavailable"})`,
      ),
    );
  }
  if (result.shareOfVoice) {
    lines.push(
      "",
      "Share of Voice:",
      ...result.shareOfVoice.entries.map(
        (entry) =>
          `- ${entry.label}${entry.isTarget ? " (target)" : ""}: ${entry.sharePct == null ? "unavailable" : `${entry.sharePct}%`} (${entry.mentions ?? "unavailable"} mentions)`,
      ),
    );
  }
  return lines.join("\n");
}

function formatPromptExplorer(
  result: z.infer<typeof promptExplorerResultSchema>,
): string {
  const sections = result.results.map((modelResult) => {
    if (modelResult.status === "error") {
      return `## ${modelResult.model} — ERROR\n${modelResult.message}`;
    }

    const citations = modelResult.citations.length
      ? `\n\nCitations:\n${modelResult.citations
          .map(
            (citation) =>
              `- ${citation.url}${citation.matchedBrand ? " (brand match)" : ""}`,
          )
          .join("\n")}`
      : "";
    const fanOutQueries = modelResult.fanOutQueries.length
      ? `\n\nFan-out queries:\n${modelResult.fanOutQueries
          .map((query) => `- ${query}`)
          .join("\n")}`
      : "";
    return [
      `## ${modelResult.model}${modelResult.modelName ? ` (${modelResult.modelName})` : ""}`,
      `Brand mentioned: ${modelResult.brandMentioned ?? "not checked"}`,
      "",
      modelResult.text || "(empty answer)",
      citations,
      fanOutQueries,
    ].join("\n");
  });

  return `Prompt: ${result.prompt}\n\n${sections.join("\n\n")}`;
}
