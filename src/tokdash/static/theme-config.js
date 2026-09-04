(function () {
  const themeConfig = Object.freeze({
    validStyleThemes: [
      "classic",
      "elevated",
      "paper",
      "liquid",
      "vibrant",
      "midnight",
      "terminal",
      "brutalist",
      "arcade",
      "studio",
      "obsidian",
      "graphite",
      "nordic",
      "amber",
      "navy",
      "soft",
      "flat",
    ],
    heatColorsMap: {
      elevated: {
        light: ["#EEF2F7", "#E0E7FF", "#C7D2FE", "#A5B4FC", "#60A5FA", "#3B82F6", "#2563EB", "#1E40AF"],
        dark: ["#172033", "#1E293B", "#1D4ED8", "#2563EB", "#3B82F6", "#60A5FA", "#93C5FD", "#BFDBFE"],
      },
      classic: {
        light: ["#EEF2F7", "#E0E7FF", "#C7D2FE", "#A5B4FC", "#60A5FA", "#3B82F6", "#2563EB", "#1E40AF"],
        dark: ["#172033", "#1E293B", "#1D4ED8", "#2563EB", "#3B82F6", "#60A5FA", "#93C5FD", "#BFDBFE"],
      },
      paper: {
        light: ["#F4EDE0", "#ECDDC8", "#DFC5A4", "#CEA475", "#B97C4A", "#9D5E35", "#7D432C", "#5C2E23"],
        dark: ["#1F1914", "#2B2018", "#3A291D", "#513523", "#70442B", "#985D38", "#C47E50", "#E8B080"],
      },
      liquid: {
        light: ["#F7FBFF", "#EAF4FF", "#D8E8FF", "#BED7FF", "#9AC0FF", "#77A6FF", "#5B8CF7", "#4472DB"],
        dark: ["#0F182B", "#15233E", "#1D345C", "#285082", "#3F74B9", "#5B8CF7", "#8DB8FF", "#C6DCFF"],
      },
      vibrant: {
        light: ["#F3F4FF", "#E4E7FF", "#D4DAFF", "#B9C2FF", "#8B8CFF", "#5B5CEB", "#4C51D6", "#373DB6"],
        dark: ["#161A34", "#1D2552", "#303C86", "#4451B0", "#5B5CEB", "#7C7EFF", "#A5B4FC", "#C7D2FE"],
      },
      midnight: {
        light: ["#F5F3FF", "#EDE9FE", "#DDD6FE", "#C4B5FD", "#A78BFA", "#8B5CF6", "#7C3AED", "#6366F1"],
        dark: ["#1A1530", "#221D3D", "#4338CA", "#4F46E5", "#6366F1", "#818CF8", "#A5B4FC", "#C7D2FE"],
      },
      terminal: {
        light: ["#F3FAF1", "#E2F2DF", "#CCE8C8", "#A7D7A6", "#74BF7B", "#3F9B56", "#1F7C41", "#10552B"],
        dark: ["#07110A", "#0C1A10", "#11331D", "#17502D", "#1F7A43", "#35B05E", "#52F78F", "#9BFFC1"],
      },
      brutalist: {
        light: ["#FFF8EA", "#FDE68A", "#FDBA74", "#FB923C", "#F97316", "#EA580C", "#1F2937", "#111827"],
        dark: ["#0A0A0A", "#171717", "#292524", "#FDE047", "#F59E0B", "#FB923C", "#F8FAFC", "#FFFFFF"],
      },
      arcade: {
        light: ["#FBF5FF", "#F3E8FF", "#E9D5FF", "#D8B4FE", "#C084FC", "#A855F7", "#DB2777", "#7C3AED"],
        dark: ["#12061F", "#1B0B2E", "#36105C", "#6D28D9", "#A855F7", "#EC4899", "#22D3EE", "#67E8F9"],
      },
      studio: {
        light: ["#F5F5F4", "#E7E5E4", "#D6D3D1", "#A8A29E", "#78716C", "#57534E", "#334155", "#111827"],
        dark: ["#101215", "#181C20", "#252B31", "#334155", "#475569", "#64748B", "#CBD5E1", "#F8FAFC"],
      },
      obsidian: {
        light: ["#F0F6F5", "#DCEDEB", "#C4E2DE", "#9FD2CC", "#6FBEB5", "#3FA89C", "#0F766E", "#0B5A54"],
        dark: ["#0C1216", "#10262A", "#0E3E3E", "#0F5A55", "#0F766E", "#14B8A6", "#2DD4BF", "#99F6E4"],
      },
      graphite: {
        light: ["#F8FAFC", "#EEF2F6", "#E2E8F0", "#CBD5E1", "#94A3B8", "#64748B", "#334155", "#0F172A"],
        dark: ["#0B0F19", "#141B29", "#1E293B", "#334155", "#475569", "#64748B", "#CBD5E1", "#F8FAFC"],
      },
      nordic: {
        light: ["#F7F6F3", "#EFEDE7", "#E2E0F4", "#C9C5EC", "#A5A1E2", "#818CF8", "#6366F1", "#4F46E5"],
        dark: ["#1A1815", "#262219", "#2E2B45", "#3F3C74", "#4F46E5", "#6366F1", "#818CF8", "#C7D2FE"],
      },
      amber: {
        light: ["#FAF6EE", "#F4EBD9", "#EBD9B4", "#E0C188", "#D2A24E", "#C2871B", "#9E6600", "#7A4E00"],
        dark: ["#100D08", "#1D1608", "#332408", "#4A3400", "#6B4A00", "#8A5E00", "#D48F00", "#FFC000"],
      },
      navy: {
        light: ["#F0F4FA", "#E1EBF5", "#CBDCF0", "#A8C4E6", "#7FA6D9", "#5485C9", "#1D4ED8", "#1E3A8A"],
        dark: ["#0A0E1A", "#101A30", "#16264A", "#1D3560", "#2547A0", "#2B5CE6", "#3B82F6", "#93C5FD"],
      },
      soft: {
        light: ["#F4F4F6", "#E9E9F0", "#DBDBEA", "#C5C5E0", "#A9A9D8", "#8B8BD0", "#6366F1", "#4F46E5"],
        dark: ["#0F0F12", "#17171D", "#23233A", "#34345C", "#4F46E5", "#6366F1", "#818CF8", "#C7D2FE"],
      },
      flat: {
        light: ["#FCFCFC", "#F5F5F5", "#EAEAEA", "#D4D4D4", "#A3A3A3", "#737373", "#404040", "#000000"],
        dark: ["#0A0A0A", "#141414", "#1F1F1F", "#2E2E2E", "#525252", "#8C8C8C", "#D4D4D4", "#FFFFFF"],
      },
    },
    chartPaletteMap: {
      elevated: {
        light: ["#1E40AF", "#3B82F6", "#0F766E", "#F59E0B", "#64748B", "#94A3B8"],
        dark: ["#60A5FA", "#93C5FD", "#34D399", "#FBBF24", "#CBD5E1", "#94A3B8"],
      },
      classic: {
        light: ["#1E40AF", "#3B82F6", "#0F766E", "#F59E0B", "#64748B", "#94A3B8"],
        dark: ["#60A5FA", "#93C5FD", "#34D399", "#FBBF24", "#CBD5E1", "#94A3B8"],
      },
      paper: {
        light: ["#3F4B5C", "#8A6F4D", "#C27C2C", "#2F7D57", "#B36A3A", "#7C6B5A"],
        dark: ["#E3D3B4", "#C6A878", "#D49A4A", "#86EFAC", "#FDBA74", "#9CA3AF"],
      },
      liquid: {
        light: ["#5B8CF7", "#7DD3FC", "#A78BFA", "#34D399", "#FB7185", "#F59E0B"],
        dark: ["#8DB8FF", "#67E8F9", "#C4B5FD", "#34D399", "#FDA4AF", "#FBBF24"],
      },
      vibrant: {
        light: ["#5B5CEB", "#60A5FA", "#10B981", "#F59E0B", "#FB7185", "#64748B"],
        dark: ["#8B8CFF", "#93C5FD", "#34D399", "#FBBF24", "#FB923C", "#CBD5E1"],
      },
      midnight: {
        light: ["#6366F1", "#8B5CF6", "#F43F5E", "#EC4899", "#14B8A6", "#94A3B8"],
        dark: ["#A5B4FC", "#C4B5FD", "#FB7185", "#F472B6", "#67E8F9", "#CBD5E1"],
      },
      terminal: {
        light: ["#1F7C41", "#35B05E", "#0F766E", "#D97706", "#7CFFCF", "#B8FFD0"],
        dark: ["#52F78F", "#7CFFCF", "#34D399", "#FBBF24", "#FDBA74", "#D1FAE5"],
      },
      brutalist: {
        light: ["#111827", "#F97316", "#2563EB", "#16A34A", "#FDE047", "#6B7280"],
        dark: ["#F8FAFC", "#FDE047", "#FB923C", "#60A5FA", "#4ADE80", "#A1A1AA"],
      },
      arcade: {
        light: ["#8B5CF6", "#EC4899", "#22D3EE", "#F59E0B", "#34D399", "#6366F1"],
        dark: ["#C084FC", "#F472B6", "#67E8F9", "#FBBF24", "#34D399", "#A5B4FC"],
      },
      studio: {
        light: ["#111827", "#475569", "#2563EB", "#0F766E", "#DC2626", "#A8A29E"],
        dark: ["#F8FAFC", "#CBD5E1", "#60A5FA", "#34D399", "#FB7185", "#78716C"],
      },
      obsidian: {
        light: ["#0F766E", "#0369A1", "#B45309", "#6D28D9", "#BE185D", "#5F7370"],
        dark: ["#2DD4BF", "#38BDF8", "#F59E0B", "#A78BFA", "#F472B6", "#94A3B8"],
      },
      graphite: {
        light: ["#0F172A", "#2563EB", "#10B981", "#C2410C", "#8B5CF6", "#64748B"],
        dark: ["#F8FAFC", "#3B82F6", "#34D399", "#F97316", "#A78BFA", "#94A3B8"],
      },
      nordic: {
        light: ["#4F46E5", "#0E7490", "#047857", "#B45309", "#BE185D", "#70655B"],
        dark: ["#818CF8", "#22D3EE", "#34D399", "#FBBF24", "#F472B6", "#A89F95"],
      },
      amber: {
        light: ["#9E6600", "#0369A1", "#047857", "#A3341B", "#BE185D", "#6E5D4F"],
        dark: ["#FFC000", "#00E5FF", "#4EDBA4", "#FF9E64", "#F472B6", "#A08B72"],
      },
      navy: {
        light: ["#1D4ED8", "#0369A1", "#047857", "#B45309", "#BE185D", "#3B5580"],
        dark: ["#3B82F6", "#52C7FA", "#10B981", "#F59E0B", "#F472B6", "#A3C2FA"],
      },
      soft: {
        light: ["#4F46E5", "#096F9C", "#047857", "#9E5704", "#BE185D", "#626270"],
        dark: ["#818CF8", "#38BDF8", "#34D399", "#FBBF24", "#F472B6", "#A0A0B0"],
      },
      flat: {
        light: ["#000000", "#2563EB", "#059669", "#B44409", "#DB2777", "#737373"],
        dark: ["#FFFFFF", "#3B82F6", "#34D399", "#F97316", "#F472B6", "#A3A3A3"],
      },
    },
    themeMetaColors: {
      elevated: { light: "#1E40AF", dark: "#0F172A" },
      classic: { light: "#1E40AF", dark: "#0F172A" },
      paper: { light: "#F2EBDD", dark: "#18140F" },
      liquid: { light: "#EAF4FF", dark: "#0C1629" },
      vibrant: { light: "#EEF4FF", dark: "#0F172A" },
      midnight: { light: "#6366F1", dark: "#1A1530" },
      terminal: { light: "#E7F0E4", dark: "#07110A" },
      brutalist: { light: "#F7F3E9", dark: "#101010" },
      arcade: { light: "#F6EFFF", dark: "#12061F" },
      studio: { light: "#F5F5F4", dark: "#131417" },
      obsidian: { light: "#F2F5F4", dark: "#0B0E14" },
      graphite: { light: "#F8FAFC", dark: "#0B0F19" },
      nordic: { light: "#FBFBFA", dark: "#181614" },
      amber: { light: "#FAF6EE", dark: "#0F0D0A" },
      navy: { light: "#F0F4FA", dark: "#0A0E1A" },
      soft: { light: "#F4F4F6", dark: "#0F0F12" },
      flat: { light: "#FCFCFC", dark: "#0A0A0A" },
    },
  });

  window.TOKDASH_THEME_CONFIG = themeConfig;
})();
