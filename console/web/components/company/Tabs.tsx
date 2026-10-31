"use client";
import { Empty } from "@/components/ui";
const Soon = ({ name }: { name: string }) => <Empty title={`${name}: coming next`} hint="This tab is being finished; the API endpoint is already live." />;
export const Valuation = (_: { sym: string; hasC: boolean }) => <Soon name="Valuation" />;
export const Shareholding = (_: { sym: string }) => <Soon name="Shareholding" />;
export const ActionsEstimates = (_: { sym: string; price: number }) => <Soon name="Actions & estimates" />;
export const Documents = (_: { sym: string }) => <Soon name="Documents" />;
export const Quality = (_: { sym: string }) => <Soon name="Data quality" />;
