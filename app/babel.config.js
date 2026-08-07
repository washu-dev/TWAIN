module.exports = function (api) {
  api.cache(true);
  return {
    presets: ['babel-preset-expo'],
    plugins: [
      [
        'module-resolver',
        {
          alias: {
            '@': './src',
          },
        },
      ],
      // No Reanimated plugin here on purpose -- and no app code imports Reanimated
      // any more either (see src/components/Motion.tsx for why). It stays in
      // package.json because react-native-drawer-layout, which arrives under
      // expo-router, declares it as a REQUIRED peer; unimported, it costs nothing
      // in the bundle. If it ever does get imported again, Expo SDK 56 states the
      // configuration plainly:
      // "No additional configuration is required. Reanimated Babel plugin is
      // automatically configured in babel-preset-expo when you install the
      // library." Reanimated 4 also MOVED the plugin to
      // react-native-worklets/plugin, so the legacy 'react-native-reanimated/
      // plugin' entry that used to live here was both redundant and stale --
      // with it present, useAnimatedStyle produced no animation at all on web
      // and a Reveal-wrapped tile stayed at opacity 0, i.e. invisible.
    ],
  };
};
