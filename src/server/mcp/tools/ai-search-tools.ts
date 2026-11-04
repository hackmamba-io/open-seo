import { z } from "zod";
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
} from "@/types/schemas/ai-search";
import { researchScopeSchema } from "@/shared/researchScope";
import { explorePromptTool } from "@/server/mcp/tools/explore-prompt";

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

const brandOutput = brandLookupResultSchema.shape;
const shareOfVoiceOutput = brandOutput.shareOfVoice.unwrap();
const topPageOutput = brandOutput.topPages.element;
const topQueryOutput = brandOutput.topQueries.element;

// MCP clients cache output schemas, so every object must accept new fields.
const brandVisibilityOutputSchema = brandLookupResultSchema
  .extend({
    ...optionalMetaOutputSchema,
    perPlatform: z.array(brandOutput.perPlatform.element.loose()),
    shareOfVoice: shareOfVoiceOutput
      .extend({
        entries: z.array(shareOfVoiceOutput.shape.entries.element.loose()),
      })
      .loose()
      .nullable(),
    topPages: z
      .array(
        topPageOutput
          .extend({
            keywords: z
              .array(topPageOutput.shape.keywords.element.loose())
              .max(50),
          })
          .loose(),
      )
      .max(40),
    topQueries: z
      .array(
        topQueryOutput
          .extend({
            citedSources: z
              .array(topQueryOutput.shape.citedSources.element.loose())
              .max(10),
          })
          .loose(),
      )
      .max(50),
    monthlyVolume: z.array(brandOutput.monthlyVolume.element.loose()),
  })
  .loose();

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
    const { getBrandLookup } =
      await import("@/server/features/ai-search/services/brandLookup");
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

// Keep the fork's existing tool name on the upstream implementation so auth,
// billing, country selection, and output schemas stay in sync.
export const exploreAiPromptTool = {
  ...explorePromptTool,
  name: "explore_ai_prompt",
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
