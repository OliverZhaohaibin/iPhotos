#version 330 core
in vec2 vUV;
out vec4 FragColor;

uniform sampler2D uTex;

uniform float uBrilliance;
uniform float uExposure;
uniform float uHighlights;
uniform float uShadows;
uniform float uBrightness;
uniform float uContrast;
uniform float uBlackPoint;
uniform float uSaturation;
uniform float uVibrance;
uniform float uColorCast;
uniform vec3  uGain;
uniform vec4  uBWParams;
uniform bool  uBWEnabled;
uniform float uTime;
uniform vec2  uViewSize;
uniform vec2  uTexSize;
uniform float uScale;
uniform vec2  uPan;
uniform float uImgScale;
uniform vec2  uImgOffset;
uniform float uCropCX;
uniform float uCropCY;
uniform float uCropW;
uniform float uCropH;

float clamp01(float x) { return clamp(x, 0.0, 1.0); }

float luminance(vec3 color) {
    // Use Rec. 709 coefficients to match the CPU preview pipeline.
    return dot(color, vec3(0.2126, 0.7152, 0.0722));
}

float apply_channel(float value,
                    float exposure,
                    float brightness,
                    float brilliance,
                    float highlights,
                    float shadows,
                    float contrast_factor,
                    float black_point)
{
    float adjusted = value + exposure + brightness;
    float mid_distance = value - 0.5;
    adjusted += brilliance * (1.0 - pow(mid_distance * 2.0, 2.0));

    if (adjusted > 0.65) {
        float ratio = (adjusted - 0.65) / 0.35;
        adjusted += highlights * ratio;
    } else if (adjusted < 0.35) {
        float ratio = (0.35 - adjusted) / 0.35;
        adjusted += shadows * ratio;
    }

    adjusted = (adjusted - 0.5) * contrast_factor + 0.5;

    if (black_point > 0.0)
        adjusted -= black_point * (1.0 - adjusted);
    else if (black_point < 0.0)
        adjusted -= black_point * adjusted;

    return clamp01(adjusted);
}

vec3 apply_color_transform(vec3 rgb,
                           float saturation,
                           float vibrance,
                           float colorCast,
                           vec3 gain)
{
    vec3 mixGain = (1.0 - colorCast) + gain * colorCast;
    rgb *= mixGain;

    float luma = dot(rgb, vec3(0.299, 0.587, 0.114));
    vec3  chroma = rgb - vec3(luma);
    float sat_amt = 1.0 + saturation;
    float vib_amt = 1.0 + vibrance;
    float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);
    float chroma_scale = sat_amt * (1.0 + (vib_amt - 1.0) * w);
    chroma *= chroma_scale;
    return clamp(vec3(luma) + chroma, 0.0, 1.0);
}

float gamma_neutral_signed(float gray, float neutral_adjust) {
    // Positive values brighten neutrals, negative values darken them.
    float magnitude = 0.6 * abs(neutral_adjust);
    float gamma = (neutral_adjust >= 0.0) ? pow(2.0, -magnitude) : pow(2.0, magnitude);
    return pow(clamp(gray, 0.0, 1.0), gamma);
}

float contrast_tone_signed(float gray, float tone_adjust) {
    // Apply an S-curve controlled by the tone adjustment.
    float x = clamp(gray, 0.0, 1.0);
    float epsilon = 1e-6;
    float logit = log(clamp(x, epsilon, 1.0 - epsilon) / clamp(1.0 - x, epsilon, 1.0 - epsilon));
    float k = (tone_adjust >= 0.0) ? mix(1.0, 2.2, tone_adjust) : mix(1.0, 0.6, -tone_adjust);
    float y = 1.0 / (1.0 + exp(-logit * k));
    return clamp(y, 0.0, 1.0);
}

float grain_noise(vec2 uv, float grain_amount) {
    // Match the CPU preview noise so thumbnails and the live view stay consistent.
    if (grain_amount <= 0.0) {
        return 0.0;
    }
    float noise = fract(sin(dot(uv, vec2(12.9898, 78.233))) * 43758.5453);
    return (noise - 0.5) * 0.2 * grain_amount;
}

vec3 apply_bw(vec3 color, vec2 uv) {
    float intensity = clamp(uBWParams.x, -1.0, 1.0);
    float neutrals = clamp(uBWParams.y, -1.0, 1.0);
    float tone = clamp(uBWParams.z, -1.0, 1.0);
    float grain = clamp(uBWParams.w, 0.0, 1.0);

    if (abs(intensity) <= 1e-4 && abs(neutrals) <= 1e-4 && abs(tone) <= 1e-4 && grain <= 0.0) {
        return color;
    }

    float g0 = luminance(color);

    // Anchors that define the soft, neutral, and rich looks driven by the master slider.
    float g_soft = pow(g0, 0.85);
    float g_neutral = g0;
    float g_rich = contrast_tone_signed(g0, 0.35);

    float gray;
    if (intensity >= 0.0) {
        gray = mix(g_neutral, g_rich, intensity);
    } else {
        gray = mix(g_soft, g_neutral, intensity + 1.0);
    }

    gray = gamma_neutral_signed(gray, neutrals);
    gray = contrast_tone_signed(gray, tone);
    gray += grain_noise(uv * uTexSize, grain);
    gray = clamp(gray, 0.0, 1.0);

    return vec3(gray);
}

void main() {
    if (uScale <= 0.0) {
        discard;
    }

    float safeImgScale = max(uImgScale, 1e-6);

    vec2 fragPx = vec2(gl_FragCoord.x - 0.5, gl_FragCoord.y - 0.5);
    vec2 viewCentre = uViewSize * 0.5;
    vec2 viewVector = fragPx - viewCentre;
    vec2 screenVector = viewVector - uPan;
    vec2 texVector = (screenVector / uScale - uImgOffset) / safeImgScale;
    vec2 texPx = texVector + (uTexSize * 0.5);
    vec2 uv = texPx / uTexSize;

    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        discard;
    }

    uv.y = 1.0 - uv.y;

    // Apply crop in texture coordinate space
    // Calculate normalized crop boundaries
    float crop_min_x = uCropCX - uCropW * 0.5;
    float crop_max_x = uCropCX + uCropW * 0.5;
    float crop_min_y = uCropCY - uCropH * 0.5;
    float crop_max_y = uCropCY + uCropH * 0.5;
    
    // Check if current fragment's texture coordinate is outside the crop box
    if (uv.x < crop_min_x || uv.x > crop_max_x ||
        uv.y < crop_min_y || uv.y > crop_max_y) {
        discard; // Discard fragments outside the crop region
    }

    vec4 texel = texture(uTex, uv);
    vec3 c = texel.rgb;

    float exposure_term    = uExposure   * 1.5;
    float brightness_term  = uBrightness * 0.75;
    float brilliance_term  = uBrilliance * 0.6;
    float contrast_factor  = 1.0 + uContrast;

    c.r = apply_channel(c.r, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);
    c.g = apply_channel(c.g, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);
    c.b = apply_channel(c.b, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);

    c = apply_color_transform(c, uSaturation, uVibrance, uColorCast, uGain);

    if (uBWEnabled) {
        c = apply_bw(c, uv);
    }
    FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
