import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Laughtale · Chat",
  description: "합성 사용자로 내부 대화를 확인하는 로컬 채팅 실험 화면입니다.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
