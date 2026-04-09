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
        light: ["#FBF7EF", "#F2E8D8", "#E4D2B7", "#D1B18B", "#B89263", "#8E714D", "#645240", "#3F4B5C"],
        dark: ["#1A1410", "#2A211A", "#46382C", "#66503A", "#8B6B49", "#B1875A", "#D1B691", "#E8DCC7"],
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
    },
  });

  window.TOKDASH_THEME_CONFIG = themeConfig;
})();
