# TWAIN Webapp - Quick Start Guide

This is an Expo + React Native Web application that runs on iOS, Android, and Web with a shared codebase.

## Installation

```bash
cd webapp
npm install
```

## Environment Configuration

Create a `.env` file in the webapp directory with your API base URL:

```
REACT_APP_API_BASE_URL=http://localhost:8000
```

For production:
```
REACT_APP_API_BASE_URL=https://twain-api.wustl.edu
```

## Running the App

### Web
```bash
npm run web
```
Opens at `http://localhost:8081`

### iOS (macOS only)
```bash
npm run ios
```

### Android
```bash
npm run android
```

### All Platforms (Interactive Menu)
```bash
npm start
```
Then select platform:
- `w` — web
- `i` — iOS
- `a` — Android

## Project Structure

```
webapp/
├── src/
│   ├── app/              # Expo Router screens
│   ├── components/       # Reusable components
│   │   ├── Header.tsx
│   │   ├── Footer.tsx
│   │   └── TileButton.tsx
│   ├── screens/          # Full screens
│   │   └── HomeScreen.tsx
│   ├── api/              # API client
│   │   └── client.ts
│   ├── constants/        # Theme and constants
│   │   └── theme.ts
│   ├── hooks/            # React hooks
│   └── global.css        # Global styles (web)
├── app.json              # Expo configuration
├── package.json
└── tsconfig.json
```

## Features

- **Header** — WashU branding with Login and Test buttons
- **Test Button** — Calls `/api/greetings` and displays results in a popup
- **Three Tiles** — Start Simulation, Resume Workflow, Browse
- **Footer** — WashU footer with links (matches di2accelerator.wustl.edu)
- **Responsive Design** — Works on mobile and web
- **Accessibility** — WCAG 2.1 AA compliance

## WashU Branding

The app uses WashU official colors:
- **Primary**: #00205B (Dark Blue)
- **Secondary**: #E6A10A (Gold)
- **Accent**: #C6007E (Berry)

Fonts follow WashU typography guidelines from https://marcomm.washu.edu/

## API Integration

The Test button calls the FastAPI endpoint:
```
GET /api/greetings
```

Expected response:
```json
{
  "data": [
    {"message": "Hello, TWAIN!"}
  ],
  "count": 1,
  "message": "Greetings retrieved successfully"
}
```

## Development

### TypeScript
All code is TypeScript. See `tsconfig.json` for configuration.

### Styling
Uses React Native StyleSheet for cross-platform compatibility.

### State Management
Currently uses React `useState`. For complex state, consider Redux or Zustand.

## Building for Production

### Web
```bash
npm run build
```

### iOS/Android
Use Expo CLI:
```bash
eas build --platform ios
eas build --platform android
```

## Troubleshooting

### API Connection Issues
- Verify the API is running: `http://localhost:8000/api/health`
- Check `.env` has correct `REACT_APP_API_BASE_URL`
- Ensure CORS is enabled on the FastAPI backend

### Port Already in Use
```bash
npm start --clear
```

### Clear Cache
```bash
npm start -- --reset-cache
```

## Next Steps

1. Start the FastAPI backend (see `../api/QUICKSTART.md`)
2. Run the webapp: `npm run web`
3. Test the API connection: click "Test" button
4. Implement login flow for each button
5. Add navigation between screens

## Resources

- [Expo Documentation](https://docs.expo.dev/versions/v56.0.0/)
- [React Native Documentation](https://reactnative.dev/)
- [WashU Branding Guidelines](https://marcomm.washu.edu/)
- [WashU Accessibility Guidelines](https://digitalaccessibility.wustl.edu/)
