import { useColorScheme as useColorSchemeRN, Platform } from 'react-native';

export function useColorScheme() {
  const scheme = useColorSchemeRN();

  if (Platform.OS === 'web') {
    return 'light';
  }

  return scheme ?? 'light';
}
