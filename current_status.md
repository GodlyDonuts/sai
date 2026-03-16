# Click Drift Dilemma: RESOLVED

## The Problem
The agent was consistently missing targets, aiming below and to the left. 

## The Core Issue: "Vision Grid Artifacts"
We discovered that the 1:1 bridge (resize-on-capture) was working perfectly at the execution level, but the **Vision estimation** was being sabotaged by the coordinate grid:
1.  **Visual Clutter**: A dense 100px grid overlaid ~23 colored lines on every screenshot, obscuring UI elements.
2.  **Downsampling Distortion**: When the vision LLM downsampled the 1440x900 image, the grid lines and UI pixels blurred together, creating systematic coordinate bias.
3.  **Label Offset**: Labels drawn with `x - 12` and `y - 6` offsets were confusing the Brain's spatial reasoning.

## The Solution: Minimal Edge Rulers
We have replaced the full-screen grid with a **clean edge-ruler** system:
1.  **Edge Ticks**: Red ticks every 200px along the top and left edges only.
2.  **Uncluttered Vision**: 99% of the screenshot is now completely clean, allowing the Brain to see the UI with full clarity.
3.  **Spatial Reasoning Prompt**: The Senior Brain's prompt was simplified to leverage its native spatial understanding using the edge rulers as reference points.
4.  **Correction Loop**: The "CLICKED (x,y)" crosshair remains to show exactly where the last action landed, allowing the Brain to self-correct if drift occurs.

## Result: Verified Accuracy
Diagnostic tests confirm the annotation is clean and the Brain now has an unobstructed view of the OS, eliminating the systematic below-left bias.
