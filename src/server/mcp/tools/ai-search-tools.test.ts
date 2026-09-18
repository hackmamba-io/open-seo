import { z } from "zod";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { objectSchema } from "@/server/mcp/output-schemas";
import {
  exploreAiPromptTool,
  getAiBrandVisibilityTool,
} from "./ai-search-tools";
import { makeToolContext, textContent } from "./tool-test-support";

const mocks = vi.hoisted(() => ({
  getProjectForOrganization: vi.fn(),
  getBrandLookup: vi.fn(),
  explorePrompt: vi.fn(),
}));

vi.mock("@/server/features/projects/services/ProjectService", () => ({
  ProjectService: {
    getProjectForOrganization: mocks.getProjectForOrganization,
  },
}));

vi.mock("@/server/auth/repositories/AuthRepository", () => ({
  AuthRepository: { getMembership: vi.fn() },
}));

vi.mock("@/server/features/ai-search/services/brandLookup", () => ({
  getBrandLookup: mocks.getBrandLookup,
}));

vi.mock("@/server/features/ai-search/services/promptExplorer", () => ({
  explorePrompt: mocks.explorePrompt,
}));

const toolContext = makeToolContext();

const brandResult = {
  query: "Hackmamba",
  detectedTargetType: "keyword" as const,
  resolvedTarget: "Hackmamba",
  scope: null,
  aggregatesAreDomainLevel: false,
  fetchedAt: "2026-09-18T10:00:00.000Z",
  hasData: true,
  totalMentions: 12,
  totalAiSearchVolume: 900,
  perPlatform: [
    {
      platform: "chat_gpt" as const,
      status: "success" as const,
      mentions: 7,
      aiSearchVolume: 500,
    },
    {
      platform: "google" as const,
      status: "success" as const,
      mentions: 5,
      aiSearchVolume: 400,
    },
  ],
  shareOfVoice: {
    platforms: ["chat_gpt" as const, "google" as const],
    entries: [
      { label: "Hackmamba", isTarget: true, mentions: 12, sharePct: 60 },
      { label: "Competitor", isTarget: false, mentions: 8, sharePct: 40 },
    ],
  },
  topPages: [
    {
      url: "https://hackmamba.io/services",
      domain: "hackmamba.io",
      platform: "chat_gpt" as const,
      mentions: 4,
      capturedVolume: 300,
      keywords: [{ question: "best agency", aiSearchVolume: 100 }],
    },
  ],
  topQueries: [
    {
      question: "best technical writing agency",
      platform: "chat_gpt" as const,
      aiSearchVolume: 100,
      firstSeenAt: null,
      lastSeenAt: null,
      citedSources: [
        {
          url: "https://hackmamba.io/services",
          domain: "hackmamba.io",
          title: "Services",
        },
      ],
      brandsMentioned: ["Hackmamba"],
    },
  ],
  monthlyVolume: [{ year: 2026, month: 8, volume: 900 }],
};

const promptResult = {
  prompt: "best technical writing agency",
  highlightBrand: "Hackmamba",
  fetchedAt: "2026-09-18T10:00:00.000Z",
  results: [
    {
      status: "success" as const,
      model: "perplexity" as const,
      modelName: "sonar-reasoning-pro",
      text: "Hackmamba is one option.",
      citations: [
        {
          url: "https://hackmamba.io/services",
          domain: "hackmamba.io",
          title: "Services",
          matchedBrand: true,
        },
      ],
      fanOutQueries: ["technical writing firms"],
      brandMentioned: true,
      outputTokens: 42,
      webSearch: true,
    },
  ],
};

describe("AI Search MCP tools", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getProjectForOrganization.mockResolvedValue({
      id: "project_1",
      locationCode: 2826,
      languageCode: "en",
    });
  });

  it("delegates brand visibility to getBrandLookup with project auth and market defaults", async () => {
    mocks.getBrandLookup.mockResolvedValue(brandResult);
    const args = z.object(getAiBrandVisibilityTool.config.inputSchema).parse({
      projectId: "project_1",
      query: "Hackmamba",
      competitors: ["Competitor"],
    });

    const result = await getAiBrandVisibilityTool.handler(args, toolContext);

    expect(mocks.getBrandLookup).toHaveBeenCalledWith(
      {
        projectId: "project_1",
        query: "Hackmamba",
        competitors: ["Competitor"],
        scope: undefined,
        locationCode: 2826,
        languageCode: "en",
      },
      expect.objectContaining({
        projectId: "project_1",
        organizationId: "org_123",
      }),
    );
    expect(result.structuredContent).toMatchObject({
      totalMentions: 12,
      topPages: [{ url: "https://hackmamba.io/services" }],
      meta: { projectId: "project_1" },
    });
    expect(result.structuredContent.shareOfVoice?.entries[0]).toMatchObject({
      label: "Hackmamba",
      isTarget: true,
    });
    expect(textContent(result)).toContain("Top cited pages:");
    expect(textContent(result)).toContain("Share of Voice:");
    expect(
      objectSchema(getAiBrandVisibilityTool.config.outputSchema).safeParse(
        result.structuredContent,
      ).success,
    ).toBe(true);
  });

  it("delegates prompt exploration and preserves model answers and citations", async () => {
    mocks.explorePrompt.mockResolvedValue(promptResult);
    const args = z.object(exploreAiPromptTool.config.inputSchema).parse({
      projectId: "project_1",
      prompt: "best technical writing agency",
      models: ["perplexity"],
      highlightBrand: "Hackmamba",
    });

    const result = await exploreAiPromptTool.handler(args, toolContext);

    expect(mocks.explorePrompt).toHaveBeenCalledWith(
      {
        projectId: "project_1",
        prompt: "best technical writing agency",
        models: ["perplexity"],
        highlightBrand: "Hackmamba",
        webSearch: true,
        webSearchCountryCode: undefined,
      },
      expect.objectContaining({ projectId: "project_1" }),
    );
    expect(result.structuredContent).toMatchObject(promptResult);
    expect(textContent(result)).toContain("sonar-reasoning-pro");
    expect(textContent(result)).toContain("Hackmamba is one option.");
    expect(textContent(result)).toContain("https://hackmamba.io/services");
    expect(textContent(result)).toContain("technical writing firms");
    expect(
      objectSchema(exploreAiPromptTool.config.outputSchema).safeParse(
        result.structuredContent,
      ).success,
    ).toBe(true);
  });

  it("blocks an unknown project before calling either AI Search service", async () => {
    mocks.getProjectForOrganization.mockResolvedValue(null);

    await expect(
      getAiBrandVisibilityTool.handler(
        {
          projectId: "missing_project",
          query: "Hackmamba",
        },
        toolContext,
      ),
    ).rejects.toThrow();
    expect(mocks.getBrandLookup).not.toHaveBeenCalled();
  });
});
