/** @type {import('tailwindcss').Config} */
module.exports = {
  // Only the new CRM screens use Tailwind; the old sheet pages keep style.css.
  content: ["../templates/crm/**/*.html", "../crm/**/*.py"],
  theme: {
    extend: {
      colors: {
        // agitare.com.br identity, same as the Agitare Core
        brand: { DEFAULT: "#21317A", dark: "#0C0F1E" },
        accent: { DEFAULT: "#F02D62", text: "#D61F55" },
      },
    },
  },
  plugins: [],
};
