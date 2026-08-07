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
      // No Reanimated plugin here on purpose. Expo SDK 56 states it plainly:
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
