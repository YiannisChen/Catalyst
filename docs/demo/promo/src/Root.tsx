import type React from 'react';
import { Composition } from 'remotion';
import { Edit, FPS, cutFrames } from './Edit';

export const Root: React.FC = () => (
  <>
    <Composition id="Readme" component={Edit} durationInFrames={cutFrames('readme')} fps={FPS} width={1920} height={1080} defaultProps={{ cut: 'readme' as const }} />
    <Composition id="Data" component={Edit} durationInFrames={cutFrames('data')} fps={FPS} width={1920} height={1080} defaultProps={{ cut: 'data' as const }} />
  </>
);
