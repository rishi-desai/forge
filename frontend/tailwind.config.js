/** Palette: calm desaturated navy-slate. Amber is reserved for the
 *  conviction-override system only — amber on screen = "unusual bot behavior". */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink:     "#0C111D",
        panel:   "#131A2A",
        well:    "#0F1522",
        line:    "#1F2A40",
        text:    "#E6ECF7",
        mute:    "#8A99B5",
        faint:   "#5A6A87",
        gain:    "#34D399",
        loss:    "#F87171",
        watch:   "#F5A623",
        info:    "#6CA8FF",
      },
      fontFamily: {
        sys: ["-apple-system","BlinkMacSystemFont","Segoe UI","Roboto","Helvetica","Arial","sans-serif"],
        mono: ["ui-monospace","SFMono-Regular","Menlo","Consolas","monospace"],
      },
    },
  },
  plugins: [],
};
