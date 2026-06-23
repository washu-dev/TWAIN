import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { Spacing } from '@/constants/theme';

interface TileButtonProps {
  title: string;
  description: string;
  onPress?: () => void;
  accentColor?: string;
}

export const TileButton: React.FC<TileButtonProps> = ({
  title,
  description,
  onPress,
  accentColor = '#BA0C2F',
}) => {
  return (
    <TouchableOpacity
      style={[styles.container, { borderLeftColor: accentColor }]}
      onPress={onPress}
      activeOpacity={0.75}
      accessible={true}
      accessibilityLabel={title}
      accessibilityHint={description}
      accessibilityRole="button"
    >
      <View style={[styles.accentBar, { backgroundColor: accentColor }]} />

      <View style={styles.contentContainer}>
        <Text style={styles.title}>{title}</Text>
        <Text style={styles.description}>{description}</Text>
      </View>

      <Text style={[styles.arrow, { color: accentColor }]}>→</Text>
    </TouchableOpacity>
  );
};

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#FFFFFF',
    marginBottom: Spacing.three,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: '#DDDDDD',
    overflow: 'hidden',
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.08,
    shadowRadius: 4,
    elevation: 2,
  },
  accentBar: {
    width: 6,
    alignSelf: 'stretch',
  },
  contentContainer: {
    flex: 1,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.three,
  },
  title: {
    fontSize: 17,
    fontWeight: '700',
    color: '#1A1A1A',
    marginBottom: Spacing.one,
  },
  description: {
    fontSize: 13,
    color: '#5A5A5A',
    lineHeight: 18,
  },
  arrow: {
    fontSize: 22,
    paddingRight: Spacing.three,
  },
});
