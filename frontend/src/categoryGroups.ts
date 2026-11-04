// Presentation groups. The server still supplies the labels, options and defaults.
export const CATEGORY_GROUPS = [
  { id: "tastes", title: "Tastes and interests", icon: "✨", ids: ["interest", "style", "favorite_brand"] },
  { id: "gifts", title: "Gift ideas and exclusions", icon: "🎁", ids: ["wish", "dislike", "owned_item"] },
  { id: "occasions", title: "Occasions and memories", icon: "📅", ids: ["birthday", "anniversary", "life_event", "sentimental"] },
  { id: "personal", title: "Personal details", icon: "🔒", ids: ["location", "size", "diet_exclusion", "sensitive"] },
];

const GROUPED_CATEGORY_IDS = new Set(CATEGORY_GROUPS.flatMap(({ ids }) => ids));

export const OTHER_CATEGORY_GROUP = { id: "other", title: "Other details", icon: "📋" };

export function groupCategories<T>(items: readonly T[], categoryId: (item: T) => string) {
  return [
    ...CATEGORY_GROUPS.map(({ id, title, icon, ids }) => ({
      id, title, icon, items: items.filter((item) => ids.includes(categoryId(item))),
    })),
    {
      ...OTHER_CATEGORY_GROUP,
      items: items.filter((item) => !GROUPED_CATEGORY_IDS.has(categoryId(item))),
    },
  ];
}
