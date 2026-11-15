\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{academic-manuscript}[2026/01/01 Sample Academic Manuscript Class]

\DeclareOption*{\PassOptionsToClass{\CurrentOption}{report}}
\ProcessOptions\relax
\LoadClass[11pt,a4paper]{report}

\RequirePackage{amsmath}
\RequirePackage{graphicx}
\RequirePackage{booktabs}
\RequirePackage{manuscriptstyle}

\newcommand{\manuscriptinstitution}{Example Research Institute}
\newcommand{\manuscriptdepartment}{Department of Computer Science}
\newcommand{\manuscriptadvisor}[1]{\def\@manuscriptadvisor{#1}}
\manuscriptadvisor{Prof.\ Dr.\ Samira Patel}

\newcommand{\makemanuscripttitle}{%
  \begin{titlepage}
    \centering
    \vspace*{2cm}
    {\Large \textsc{\manuscriptinstitution}\par}
    \vspace{0.4cm}
    {\large \manuscriptdepartment\par}
    \vspace{2cm}
    {\huge\bfseries \@title \par}
    \vspace{1.5cm}
    {\Large \@author \par}
    \vspace{1.5cm}
    {\normalsize Advised by \@manuscriptadvisor \par}
    \vfill
    {\normalsize \manuscriptbadge \par}
    \vspace{0.5cm}
    {\normalsize \@date \par}
  \end{titlepage}
}
\endinput
