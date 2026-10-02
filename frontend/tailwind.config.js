/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: '#FF1717',
        secondary: '#242424',
        background: '#000000',
        surface: '#1E1E1E',
        stark: {
          red: '#FF1717',
          black: '#000000',
          surface: '#1E1E1E',
          elevated: '#242424',
        },
      },
    },
  },
  plugins: [],
};
